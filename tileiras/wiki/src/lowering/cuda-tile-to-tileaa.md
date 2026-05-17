# Lowering: cuda_tile to nv_tileaa

## Abstract

`ConvertCudaTileToTileAA` is the first lowering pass in the tileiras pipeline and the only one that translates from a publicly-defined dialect. It rewrites `cuda_tile` — the bytecode-input form users author against — into the internal `nv_tileaa` dialect every subsequent pass operates on. No `cuda_tile.*` operation may survive this pass.

The conversion is partial. The pass loads six legal dialects, marks `cuda_tile` illegal, attaches a dynamic-legality predicate to `ub.poison`, registers three type-conversion functor pairs, and applies a pattern bank assembled by three independent populators in a fixed order.

## Pass Driver

`runOnOperation` reads the stored `--compute-capability` option, builds the conversion target, populates three pattern groups in order, runs the PDL fallback, and invokes `applyPartialConversion`. Two user-facing diagnostics escape: `"invalid or missing --compute-capability option"` when the option parses as malformed, and `"failed to convert cuda_tile to nv_tileaa"` when partial conversion fails to legalise every `cuda_tile.*` op.

```c
LogicalResult convertCudaTileToTileAA(ModuleOp mod, ComputeCapability cc) {
    if (!cc.valid()) {
        return emit("invalid or missing --compute-capability option");
    }

    RewritePatternSet patterns;
    populatePartA(patterns);                 // arithmetic, comparison, conversion, indexing, control flow
    populatePartB(patterns);                 // memory, pointer, token, view, partition
    populatePartC(patterns);                 // mma, reduce, scan, transcendental specialists

    ConversionTarget target = buildConversionTarget(mod);
    FrozenRewritePatternSet frozen;
    compilePDLPatterns(patterns, &frozen);

    if (failed(applyPartialConversion(mod, target, frozen))) {
        return emit("failed to convert cuda_tile to nv_tileaa");
    }
    return success();
}
```

The pass walks all `cuda_tile.module` operations nested in the input module before conversion. The walker is a recursive op-tree walk filtered by TypeID; collected modules land in a small inline-allocated vector sized for the common case of one nested module per bytecode input.

## Conversion Target

The conversion target builder marks six dialects legal, declares `cuda_tile` fully illegal, and attaches dynamic legality to `ub.poison`. The same target object is reused across all three populators.

```c
ConversionTarget buildConversionTarget(ModuleOp mod) {
    ConversionTarget target(*mod.getContext());

    // Fully legal — accept any op of these dialects without further checks
    target.addLegalDialect<arith::ArithDialect,
                           nv_tileaa::TileAADialect,
                           func::FuncDialect,
                           gpu::GPUDialect,
                           scf::SCFDialect,
                           math::MathDialect>();

    // Fully illegal — every cuda_tile op must be rewritten away
    target.addIllegalDialect<cuda_tile::CudaTileDialect>();

    // Dynamic legality — ub.poison is legal once its result type is an nv_tileaa primitive
    target.addDynamicallyLegalOp<ub::PoisonOp>([](ub::PoisonOp op) {
        return isLegalTileAAType(op.getResult().getType());
    });

    return target;
}
```

The type-converter materialisers handle the residual cases where partial conversion needs a bridge value while the IR is mid-rewrite. Source materialisers run when an `nv_tileaa`-typed value is needed but only the original `cuda_tile`-typed value exists; target materialisers run for the reverse direction. Both produce `builtin.unrealized_conversion_cast` operations that the next pass's reconciliation phase erases.

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

Three populators build the pattern set in fixed order. Parts A and B are mutually independent at the source level; they run sequentially so the resulting pattern-set composition stays reproducible. Part C runs after both because its patterns depend on the type-conversion and layout decisions A and B have already published.

| Part | Patterns | Role |
|------|---------:|------|
| A | ~45 | Arithmetic, comparison, conversion, indexing, structured control flow |
| B | ~34 | Memory, pointer, token, view, partition |
| C |   4 | `mmaf`, `mmai`, `reduce`, `scan` — specialists whose lowering depends on layout choices A and B locked in |

Part A registers hand-written `OpConversion` patterns (`AddIOpConversion`, `ReduceOpConversion`, and so on). Part B mixes template-generated `GenericConversion<cuda_tile::XOp, nv_tileaa::YOp>` patterns with custom view/token/entry patterns. Part C is four specialists for operations whose rewrite shape varies with the parent op's element type, accumulator location, or combiner-region structure.

## Singleton Pattern Adders

Eight pattern classes register through dedicated singleton adders rather than through the main populator bodies, because downstream callers (the CudaTileOptimizer test driver and the rsqrt/fma fusion pass) need to install them into private pattern sets without pulling in the full Part-A/B/C registration. Each adder is a single-purpose helper that allocates one `OpConversionPattern` and pushes it onto the supplied `RewritePatternSet`.

| `cuda_tile` op | Pattern class | Role |
|---|---|---|
| `cuda_tile.trunci` | `TruncIOpConversion` | Integer truncation, rewrites to `nv_tileaa.trunci` |
| `cuda_tile.rsqrt` | `RsqrtOpConversion` | Reciprocal square root, rewrites to `nv_tileaa.rsqrt` |
| `cuda_tile.maxi` | `MaxIOpConversion` | Signed integer max, rewrites to `nv_tileaa.maxi` |
| `cuda_tile.itof` | `IToFOpConversion` | Integer-to-float conversion, rewrites to `nv_tileaa.itof` |
| `cuda_tile.global` | `GlobalOpConversion` | Global symbol declaration, rewrites to `nv_tileaa.global` |
| `cuda_tile.fma` | `FmaOpConversion` | Fused multiply-add, rewrites to `nv_tileaa.fma` |
| `cuda_tile.constant` | `ConstantOpConversion` | Tile constant, rewrites to `nv_tileaa.constant_tensor` or `nv_tileaa.splat` |
| `cuda_tile.assume` | `AssumeOpConversion` | Assumption hint, rewrites to `nv_tileaa.assume` |

Each rewrite has the same one-to-one shape:

```mlir
%r = cuda_tile.rsqrt %x : tensor<8x64xf32>
   ↓
%r = nv_tileaa.rsqrt %x : tensor<8x64xf32>
```

The eight ops never appear in the main populator rosters; the singleton adders are the only registration path that brings them into a pattern set.

### `cuda_tile.trunci` Walk

`TruncIOpConversion` is the canonical type-narrowing rewrite. The operand is an integer tile and the result is a narrower integer tile of the same shape. The rewrite keeps the operand SSA value verbatim, swaps the op mnemonic, and asks the TypeConverter for the result type:

```mlir
// Before
%narrow = cuda_tile.trunci %wide : !cuda_tile.tile<128xi32> to !cuda_tile.tile<128xi8>

// After
%narrow = nv_tileaa.trunci %wide : tensor<128xi32> to tensor<128xi8>
```

The operand `%wide` flows through the source materialiser when its definition has not yet rewritten — `applyPartialConversion` inserts a `builtin.unrealized_conversion_cast %wide : !cuda_tile.tile<128xi32> to tensor<128xi32>` that the downstream cast-reconciliation phase erases once both ends are TileAA-typed. No attribute hand-off is needed: `trunci` carries only its result type.

### `cuda_tile.fma` Walk

`FmaOpConversion` is a three-operand floating-multiply-add. All three operands share the source tile type and the result has the same shape:

```mlir
// Before
%r = cuda_tile.fma %a, %b, %c { fastmath = #cuda_tile.fastmath<contract> }
    : !cuda_tile.tile<8x64xf32>

// After
%r = nv_tileaa.fma %a, %b, %c { fastmath = #nv_tileaa.fastmath<contract> }
    : tensor<8x64xf32>
```

The `fastmath` attribute carries verbatim through the rewrite, but its dialect-qualified attribute kind changes from `#cuda_tile.fastmath<...>` to `#nv_tileaa.fastmath<...>` because each dialect publishes its own attribute. The rewriter looks the source attribute up in the TileAA dialect's attribute registry by short-name match and reconstructs the typed attribute; identical short names with identical underlying values are required to round-trip, otherwise the verifier on the new op rejects the result.

## Type-Converter Materialisers

Three type-converter functor pairs register before the populators run. Each pair combines an `addConversion` callback (called when the converter sees the source type) with an `addMaterialization` callback (called when partial conversion needs a bridge value while the IR is mid-rewrite). Materialisations should not survive later canonicalisation — the reconciliation phase in the next pass erases them.

| Source type | Target type | Materialiser direction |
|---|---|---|
| `cuda_tile::TileType` | `llvm.struct<...>` (descriptor shape) | source — produces an `nv_tileaa` value when only a `cuda_tile` value exists |
| `cuda_tile::PointerType` | `llvm.ptr` | target — produces a `cuda_tile` value when only an `nv_tileaa` value exists |
| `cuda_tile::TokenType` | `llvm.token` | source — produces an `nv_tileaa` value when only a `cuda_tile` value exists |

Splitting source from target materialisers preserves token ordering and view identity for the scheduler, which still needs to reason about memory dependences before later NVVM lowering flattens tokens into integers. A purely-symmetric materialiser pair would lose the directional information the dialect-conversion engine uses to pick the right cast.

## Block-Argument Type Flow

Region-bearing operations (`cuda_tile.reduce`, `cuda_tile.scan`, structured control flow that carries `cuda_tile`-typed iteration arguments) need block-argument types converted in the same step as their parent op. The standard inline-region helper does not see the pass's type converter, so the region-rewriting patterns construct their replacement operations explicitly:

```c
LogicalResult lowerRegionOp(Operation *src, OperationName dst,
                            ConversionPatternRewriter &rw,
                            const TypeConverter &types) {
    SmallVector<Value> operands;
    if (failed(types.convertOperands(src->getOperands(), operands)))
        return failure();

    SmallVector<Type> resultTypes;
    if (failed(types.convertTypes(src->getResultTypes(), resultTypes)))
        return failure();

    OperationState state(src->getLoc(), dst);
    state.addOperands(operands);
    state.addTypes(resultTypes);
    state.addAttributes(src->getAttrs());

    for (Region &region : src->getRegions()) {
        Region *newRegion = state.addRegion();
        rw.inlineRegionBefore(region, *newRegion, newRegion->begin());
        if (failed(rw.convertRegionTypes(newRegion, types)))
            return failure();
    }

    Operation *replacement = rw.create(state);
    rw.replaceOp(src, replacement->getResults());
    return success();
}
```

`convertRegionTypes` walks the block-argument list of every block in the region and rewrites types through the same converter the parent op uses. Without this step, the parent op verifies against post-conversion operand types but its region terminator yields pre-conversion types — a signature mismatch the next-stage verifier reports without enough context to diagnose properly.

## Part C Specialists

Part C registers four specialists that depend on layout decisions made by Parts A and B. Each takes a `cuda_tile` op whose lowering shape is parameterised by element type, layout intent, or combiner-region structure, and emits the matching `nv_tileaa` form.

### `cuda_tile.mmaf` and `cuda_tile.mmai`

The float and integer matrix-multiply-accumulate ops rewrite to `nv_tileaa.dot` with the element-type-specific attribute set. The rewriter selects FP rounding mode and accumulator precision from the source op's attributes.

```mlir
%c' = cuda_tile.mmaf %a, %b, %c { fastmath = "contract" }
   : tensor<128x64xf16>, tensor<64x128xf16>, tensor<128x128xf32>
   ↓
%c' = nv_tileaa.dot %a, %b, %c { input_precision = "tf32", fastmath = "contract" }
   : tensor<128x64xf16>, tensor<64x128xf16>, tensor<128x128xf32>
```

### `cuda_tile.reduce` Worked Example

`cuda_tile.reduce` carries a combiner region whose block arguments are accumulator-typed and whose terminator yields the next accumulator value. The rewriter walks the region, converts block-argument types through the shared `TypeConverter`, and rebuilds the op as `nv_tileaa.reduce` with the converted region body.

Input:

```mlir
%sum = cuda_tile.reduce %values { axis = 1 : i32 } : tensor<8x64xf32> -> tensor<8xf32> {
  ^bb0(%acc: !cuda_tile.tile<f32>, %val: !cuda_tile.tile<f32>):
    %s = cuda_tile.addf %acc, %val : !cuda_tile.tile<f32>
    cuda_tile.yield %s : !cuda_tile.tile<f32>
}
```

The pattern converts the parent op's operand and result types, inlines the region, then walks the new region's blocks to convert each block-argument type:

```mlir
%sum = nv_tileaa.reduce %values { axis = 1 : i32 } : tensor<8x64xf32> -> tensor<8xf32> {
  ^bb0(%acc: f32, %val: f32):
    %s = nv_tileaa.addf %acc, %val : f32
    nv_tileaa.yield %s : f32
}
```

Block-argument types `!cuda_tile.tile<f32>` become `f32` because the `TileType` conversion strips the dialect wrapper; the terminator and combiner body rewrite recursively under the same partial-conversion driver, since `cuda_tile.addf` and `cuda_tile.yield` are in the illegal dialect and match Part A patterns.

If the rewriter forgot to convert block-argument types, the parent `nv_tileaa.reduce` would have `f32` operands at the outer signature but the inner region's `^bb0` would still bind `!cuda_tile.tile<f32>` — the verifier would reject the operation with a signature mismatch the next-stage diagnostics cannot localise back to this pass.

### `cuda_tile.scan` Worked Example

`cuda_tile.scan` follows the same shape as reduce but produces a tensor of the same rank as the input — every output element is the cumulative reduction of the prefix of input elements along the scan axis. The rewriter applies identical region-conversion logic, only changing the parent op's mnemonic and keeping the result rank equal to the input rank.

Input:

```mlir
%prefix = cuda_tile.scan %values { axis = 1 : i32, inclusive = true }
    : !cuda_tile.tile<8x64xf32> -> !cuda_tile.tile<8x64xf32> {
  ^bb0(%acc: !cuda_tile.tile<f32>, %elem: !cuda_tile.tile<f32>):
    %sum = cuda_tile.addf %acc, %elem : !cuda_tile.tile<f32>
    cuda_tile.yield %sum : !cuda_tile.tile<f32>
}
```

Output:

```mlir
%prefix = nv_tileaa.scan %values { axis = 1 : i32, inclusive = true }
    : tensor<8x64xf32> -> tensor<8x64xf32> {
  ^bb0(%acc: f32, %elem: f32):
    %sum = nv_tileaa.addf %acc, %elem : f32
    nv_tileaa.yield %sum : f32
}
```

The `axis` and `inclusive` attributes carry verbatim; block-argument types unwrap from `!cuda_tile.tile<f32>` to `f32` via the TileType converter, and the inner `cuda_tile.addf` / `cuda_tile.yield` rewrite recursively under Part A patterns matched by the same partial-conversion driver.

### Transcendental Specialists

The transcendental specialists (`cuda_tile.exp2`, `cuda_tile.log2`, `cuda_tile.sin`, `cuda_tile.cos`, `cuda_tile.tanh`) rewrite to `nv_tileaa` counterparts but additionally attach the `fastmath` flag derived from the source op's attribute dictionary. The flag controls whether downstream lowering selects the `__nv_*` precise libdevice variant or the `__nv_fast_*` approximate variant.

## Tokens and Atomics

Token-aware operations stay explicit in the IR rather than collapsing immediately to NVVM. Loads, stores, atomic compare-and-swap, atomic read-modify-write, token creation, and token join all become `nv_tileaa` operations that still expose memory dependences. The downstream scheduler and async-pipeline passes reason about those dependences before LLVM/NVVM lowering flattens tokens into integers.

```mlir
%t = cuda_tile.token.join [%t0, %t1, %t2] : !cuda_tile.token
   ↓
%t = nv_tileaa.join_mem_token [%t0, %t1, %t2] : !nv_tileaa.token
```

Singleton joins skip the `join_mem_token` op and pass the single token through unchanged; empty joins lower to `nv_tileaa.create_null_token` so downstream ops always have a token operand to consume.

## Pipeline Handoff

The pass establishes the alias and view shapes that warp-specialized producer/consumer rewriting relies on later, but assigns no final layouts. It keeps enough structure around load/store views, atomic-token operations, and tensor partitions for TileAS layout assignment to insert `nv_tileas.view` and `nv_tileas.convert_layout` at producer and consumer boundaries. The invariant: a view produced here must still identify the same memory object, shape, layout intent, and token ordering when it reaches TileAS layout assignment.

## Failure Modes

The pass fails with a user-facing diagnostic when:

- compute capability is missing or malformed (`"invalid or missing --compute-capability option"`);
- partial conversion leaves a residual `cuda_tile.*` op (`"failed to convert cuda_tile to nv_tileaa"`);
- a type materialisation cannot bridge a value across the boundary;
- a region rewrite would produce mismatched block arguments or terminators.

## Cross-References

[Conversion / Lowering Overview](overview.md#lowering-stages) describes this pass's position in the four-stage cascade. [Shared LLVM Type Converter](pattern-set-and-typeconverter.md#shared-llvm-type-converter) documents the shared LLVM type converter that the materialiser triple here registers into. [TileAA to TileAS](tileaa-to-tileas.md) is the next lowering stage; the CopyAtom and ReduceAtom witnesses attached there preserve information this pass made explicit. [cuda_tile Op Roster](../dialects/cuda_tile/op-roster.md#op-method-surface) lists the lowering-arm classification per family that the populator order in this pass reflects; [nv_tileaa Op Roster — Memory Effects](../dialects/nv_tileaa/op-roster.md#memory-effects) gives the operand and attribute tables for the token-aware operations the singleton adders produce.
