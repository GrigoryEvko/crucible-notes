# Pattern Sets and Type Conversion

## Abstract

Tileiras lowering rides on ordinary MLIR dialect conversion: a conversion target declares what is legal, a type converter defines ABI shape, and a rewrite pattern set rewrites illegal operations until the target accepts the module. The recovered implementation hides a lot of generated pattern classes and vector-growth helpers, but the public contract is short: every lowering stage must build a complete pattern set and share one coherent LLVM type converter across TileAA, TileAS, CuTe, NVGPU, and kernel function boundaries.

This page documents that contract at reimplementation level — pattern-population algorithm, type-conversion rules, materialization hooks, address-space mapping, and the runtime descriptor shapes later passes assume.

## Pattern Population Model

Every conversion pass follows the same skeleton.

```c
LogicalResult run_dialect_conversion(Operation *root, LoweringOptions options) {
    MLIRContext *ctx = root->get_context();
    ConversionTarget target(*ctx);
    TileLLVMTypeConverter types(ctx, options);
    RewritePatternSet patterns(ctx);

    configure_target(&target, &types, options);
    populate_stage_patterns(&patterns, &types, options);
    populate_shared_cleanup_patterns(&patterns, &types, options);

    return apply_conversion(root, target, std::move(patterns));
}
```

Pattern objects come in two practical families.

| Pattern family | Use |
|---|---|
| generic one-to-one conversion | replaces an operation with the same semantic operation in the next dialect |
| dedicated conversion pattern | rewrites regions, adds target attributes, changes layout, or emits multiple operations |
| canonicalization pattern | cleans up legal operations after the dialect boundary has moved |
| one-to-N pipeline pattern | rewrites one async/pipeline operation into several LLVM/NVVM operations and values |

The generic pattern is deliberately boring: convert operands and result types, copy semantic attributes, create the destination operation, replace the source op.

```c
LogicalResult generic_remap(Operation *op, OperationName dst, Rewriter *rw, TypeConverter *types) {
    SmallVector<Value> operands = convert_operands(op->operands(), rw, types);
    SmallVector<Type> result_types = convert_types(op->result_types(), types);

    if (operands.failed() || result_types.failed()) {
        return failure();
    }

    OperationState state(op->loc(), dst);
    state.add_operands(operands);
    state.add_types(result_types);
    state.add_attributes(copy_semantic_attrs(op));
    state.add_regions(clone_regions_with_converted_block_types(op, types));

    Operation *replacement = rw->create(state);
    rw->replace_op(op, replacement->results());
    return success();
}
```

Reach for a dedicated pattern only when the generic contract is not enough: region surgery, token formation, CopyAtom or ReduceAtom propagation, compute-capability gates, descriptor packing, inline assembly, or target-specific attribute emission.

## Shared LLVM Type Converter

The Tileiras LLVM type converter is an ABI object. Every pass that may see tile, view, token, CuTe, memref, or kernel function types has to agree with it. Two passes that disagree about descriptor shape or address-space numbering produce a module that verifies but generates wrong PTX.

The converter dispatches through ordered callbacks. The first callback that recognizes a type returns the converted LLVM type.

```c
Type convert_type(Type type) {
    for (ConversionFn fn : conversions) {
        if (Optional<Type> converted = fn(type)) {
            return *converted;
        }
    }
    return Type();
}
```

| Source concept | Converted representation |
|---|---|
| integer, index, float | LLVM scalar with target width and element semantics preserved |
| vector | LLVM vector of converted element type |
| function type | function type with converted arguments and results |
| ranked memref | LLVM memref descriptor unless a bare-pointer ABI rule applies |
| unranked memref | `{rank, erased_descriptor_pointer}` |
| TileAA or TileAS memref | same descriptor family as ranked memref, with Tileiras address space |
| CuTe memref | descriptor compatible with CuTe layout lowering |
| TileAA and TileAS tiled view | small struct containing base pointer and packed layout metadata |
| async, memory, producer, and consumer tokens | `i32` |
| CuTe layout, shape, stride, swizzle, and atom types | LLVM structs or integers consumed by CuTe lowering |
| tuple and none | LLVM struct or empty marker as required by the operation |
| LLVM pointer or LLVM struct | identity conversion |

## Descriptor Layouts

The ranked memref descriptor follows the standard LLVM dialect shape:

```c
struct RankedMemRefDescriptor<T, int Rank, int AddressSpace> {
    T addrspace(AddressSpace) *allocated;
    T addrspace(AddressSpace) *aligned;
    int64_t offset;
    int64_t sizes[Rank];
    int64_t strides[Rank];
};
```

The tiled-view descriptor is compact because tiled load/store patterns and descriptor builders both consume it.

```c
struct TiledViewDescriptor<T, int AddressSpace> {
    T addrspace(AddressSpace) *base;
    uint32_t swizzle_encoding;
    uint32_t tile_dim0;
    uint32_t tile_dims1_to3[3];
};
```

Tokens are deliberately narrow. A producer/consumer token is not a pointer to runtime storage — it is an integer phase value. The low bit carries the parity consumed by wait operations; higher bits may carry a pipeline slot index.

```c
uint32_t make_pipeline_token(uint32_t slot, bool phase) {
    return (slot << 1) | (phase ? 1u : 0u);
}

uint32_t token_slot(uint32_t token) {
    return token >> 1;
}

bool token_phase(uint32_t token) {
    return (token & 1u) != 0;
}
```

## Address Spaces

Tileiras keeps memory spaces distinct all the way to LLVM pointers.

| Tileiras memory space | LLVM address space | PTX meaning |
|---|---:|---|
| register memory | 0 | virtual register values |
| global memory | 1 | `.global` |
| internal memory | 2 | compiler-internal storage |
| shared memory | 3 | `.shared` |
| constant memory | 4 | `.const` |
| local memory | 5 | `.local` |
| tensor memory | 6 | Blackwell tensor memory |
| generic pointer | 101 | NVVM generic pointer |

Address-space casts must be explicit. The converter rejects implicit transitions that would hide a semantic memory-space change — especially around TMA descriptors, shared-memory barriers, and tensor-memory operations.

## Materialization Hooks

Partial conversion sometimes needs a bridge value while only part of the IR has been lowered, so the converter supplies source and target materialization hooks. In practice both hooks create `builtin.unrealized_conversion_cast`, and cleanup removes them once all participating operations have converted.

```c
Value materialize_bridge(Type target_type, ValueRange inputs, Location loc, Rewriter *rw) {
    if (inputs.size() == 1 && inputs[0].get_type() == target_type) {
        return inputs[0];
    }

    return rw->create("builtin.unrealized_conversion_cast", loc, target_type, inputs).result(0);
}
```

Keep bridges temporary. If unrealized casts survive the final cleanup pass, the legality target should fail the module rather than rely on LLVM translation to guess.

## Pattern Quality Rules

- Generic patterns should not inspect target hardware.
- Dedicated patterns should own all target checks they introduce.
- Region-rewriting patterns must convert block argument types and terminators together.
- Patterns that copy semantic attributes must not copy stale dialect-internal caches.
- One-to-N async pipeline patterns should run only after the scheduler and layout passes have made pipeline structure explicit.
- Cleanup patterns should never erase memory-ordering operations unless the operation is outside the memory-consistency interface.

## Reimplementation Checklist

1. Define one shared LLVM type converter for all Tileiras lowering stages.
2. Register conversions in deterministic order and make identity conversions explicit.
3. Use memref, tiled-view, token, and address-space layouts exactly as documented here.
4. Keep source and target materializations temporary and remove unrealized casts at the end.
5. Split pattern population into generic remaps, dedicated rewrites, and cleanup patterns.
6. Fail conversion if an illegal dialect survives a stage boundary.

## The 43-Instantiation `GenericOpPattern<arith::*Op>` Bank

Arith lowers into TileAS through 43 byte-identical instantiations of a CRTP template `GenericOpPattern<SourceOp>`, registered by `sub_873F30` (13 127 B, called `populate_arith_GenericOpPatterns` internally). Each instantiation derives from `mlir::OpConversionPattern<SourceOp>`, occupies a 0x68 (104 B) object, and lives in the doubly-anonymous namespace `mlir::nv_tile_ir::as::{anonymous}::{anonymous}::GenericOpPattern<...>`.
Vtables live in the consecutive bank `0x59B5480..0x59B61A0`, stride `0x50` — eight 8-byte function-pointer slots plus the 16-byte Itanium RTTI prefix, exactly the Shape A layout from [Pattern Vtables and Shapes](../mlir-infra/pattern-vtables-and-shapes.md). The double-anonymous nesting is the canonical signature of a helper template declared in a detail header opened with `namespace { ... }`, then included into a `.cpp` that itself opens another `namespace { ... }` — both anon scopes survive into the typeinfo string emitted by `llvm::getTypeName<...>`.

The recovered C++ signature is small enough to fit the entire template body in one declaration. The single virtual override is `matchAndRewrite`; the remaining vtable slots are the canonical `OpConversionPattern` dispatchers (slot 2 = `ConversionPattern::rewrite` thunk, slot 3 = the no-op `match` stub) shared by every Shape A class.

```cpp
template <typename SourceOp>
class GenericOpPattern : public mlir::OpConversionPattern<SourceOp> {
 public:
  using mlir::OpConversionPattern<SourceOp>::OpConversionPattern;
  using OpAdaptor = typename SourceOp::Adaptor;
  llvm::LogicalResult matchAndRewrite(
      SourceOp op, OpAdaptor adaptor,
      mlir::ConversionPatternRewriter &rw) const override;
};
```

Each registration in `sub_873F30` is a 22-line expansion that the linker emits left-to-right in template-
argument order. `sub_4481370(&benefit_slot, 1)` stamps a default `PatternBenefit(1)` into a u16 stack slot;
`sub_44A8C20(0x68u)` allocates the 104-byte pattern object via the non-throwing `operator new` wrapper;
`sub_5EAF90` runs the `OpConversionPattern` body init shim (base ctor + context stamp at +0x60 + placeholder
vtable `&unk_5A1B4D0`); the concrete `off_59B...` vtable then immediately overwrites the placeholder; the
first-touch idiom at offsets +0x40/+0x48 stamps the `llvm::getTypeName` cache pointer and length;
`sub_5E0AA0(p+10, 0, 0)` no-ops the `SmallVector<OperationName,4> generatedOps` insert; finally, the
unique_ptr is pushed into the RewritePatternSet through either the inline fast path (write-at-end when
`end != cap`) or a per-class `_M_realloc_insert` trampoline.

The 0x68-byte object follows Shape B from
[Pattern Vtables and Shapes](../mlir-infra/pattern-vtables-and-shapes.md): vtable at +0x00, `StringRef op_name`
at +0x08, `PatternBenefit benefit` (u16) at +0x18, `kind_tag` at +0x1A, `MLIRContext*` at +0x20,
`RewritePattern` base internals across +0x28..+0x38, the `llvm::getTypeName` cache (`typeinfo_str` + length)
at +0x40..+0x48, the `SmallVector<OperationName,4> generatedOps` at +0x50, and the `TypeConverter*` slot at
+0x60. The +0x60 cell is the only one written by `OpConversionPattern`'s ctor over and above the base
`RewritePattern` cells; it carries the shared type converter through the pattern's lifetime.

### Fast Path versus Slow Path

Of the 43 instantiations, 39 push their unique_ptr through a dedicated per-class `_M_realloc_insert` trampoline (one of `sub_8636E0` through `sub_8667D0`, each exactly 343 B, byte-identical apart from the inlined move-constructor vtable offset). The remaining four — `CmpFOp`, `CeilDivSIOp`, `DivFOp`, `XOrIOp` — have their slow path inlined by the linker directly into the body of `sub_873F30` at the corresponding call site, rather than emitted as a separate weak symbol. Functional behaviour is identical: capacity-double via `sub_85ED40` (`std::vector::_M_check_len` returning `max(1, 2*size)`), allocate the new buffer via `sub_85EC90` (which routes to `sub_44A8C20(8*n)` and short-circuits to zero for `n == 0`), `memcpy` the existing slots, and free the old buffer through `j_j__free`.

### Op-Mnemonic Table

The 43 mnemonics below appear in `sub_873F30` in this exact order, which is the same order they appear in
the source-level `patterns.add<...>(typeConverter, context)` template-argument list. The vtable column gives
the rodata symbol; the trampoline column gives the per-class `_M_realloc_insert` thunk, or `(inlined)` for
the four entries whose slow path was merged into the call site.

| # | Mnemonic | Op class | vtable | trampoline |
|---:|---|---|---|---|
| 1 | `arith.cmpf` | `CmpFOp` | `off_59B5480` | (inlined) |
| 2 | `arith.cmpi` | `CmpIOp` | `off_59B54D0` | `sub_866CE0` |
| 3 | `arith.addf` | `AddFOp` | `off_59B5520` | `sub_865F60` |
| 4 | `arith.addi` | `AddIOp` | `off_59B5570` | `sub_866980` |
| 5 | `arith.andi` | `AndIOp` | `off_59B55C0` | `sub_867040` |
| 6 | `arith.bitcast` | `BitcastOp` | `off_59B5610` | `sub_866110` |
| 7 | `arith.ceildivsi` | `CeilDivSIOp` | `off_59B5660` | (inlined) |
| 8 | `arith.ceildivui` | `CeilDivUIOp` | `off_59B56B0` | `sub_866470` |
| 9 | `arith.divf` | `DivFOp` | `off_59B5700` | (inlined) |
| 10 | `arith.divsi` | `DivSIOp` | `off_59B5750` | `sub_8656F0` |
| 11 | `arith.divui` | `DivUIOp` | `off_59B57A0` | `sub_865030` |
| 12 | `arith.extf` | `ExtFOp` | `off_59B57F0` | `sub_8647C0` |
| 13 | `arith.extsi` | `ExtSIOp` | `off_59B5840` | `sub_865390` |
| 14 | `arith.extui` | `ExtUIOp` | `off_59B5890` | `sub_864970` |
| 15 | `arith.floordivsi` | `FloorDivSIOp` | `off_59B58E0` | `sub_863BF0` |
| 16 | `arith.fptosi` | `FPToSIOp` | `off_59B5930` | `sub_865540` |
| 17 | `arith.fptoui` | `FPToUIOp` | `off_59B5980` | `sub_863F50` |
| 18 | `arith.maximumf` | `MaximumFOp` | `off_59B59D0` | `sub_862E70` |
| 19 | `arith.maxnumf` | `MaxNumFOp` | `off_59B5A20` | `sub_863020` |
| 20 | `arith.maxsi` | `MaxSIOp` | `off_59B5A70` | `sub_864CD0` |
| 21 | `arith.maxui` | `MaxUIOp` | `off_59B5AC0` | `sub_863530` |
| 22 | `arith.minimumf` | `MinimumFOp` | `off_59B5B10` | `sub_8651E0` |
| 23 | `arith.minnumf` | `MinNumFOp` | `off_59B5B60` | `sub_864E80` |
| 24 | `arith.minsi` | `MinSIOp` | `off_59B5BB0` | `sub_8642B0` |
| 25 | `arith.minui` | `MinUIOp` | `off_59B5C00` | `sub_863890` |
| 26 | `arith.mulf` | `MulFOp` | `off_59B5C50` | `sub_865DB0` |
| 27 | `arith.muli` | `MulIOp` | `off_59B5CA0` | `sub_863A40` |
| 28 | `arith.negf` | `NegFOp` | `off_59B5CF0` | `sub_8631D0` |
| 29 | `arith.ori` | `OrIOp` | `off_59B5D40` | `sub_864B20` |
| 30 | `arith.remf` | `RemFOp` | `off_59B5D90` | `sub_864100` |
| 31 | `arith.remsi` | `RemSIOp` | `off_59B5DE0` | `sub_864610` |
| 32 | `arith.remui` | `RemUIOp` | `off_59B5E30` | `sub_864460` |
| 33 | `arith.select` | `SelectOp` | `off_59B5E80` | `sub_8658A0` |
| 34 | `arith.shli` | `ShLIOp` | `off_59B5ED0` | `sub_866E90` |
| 35 | `arith.shrsi` | `ShRSIOp` | `off_59B5F20` | `sub_8662C0` |
| 36 | `arith.shrui` | `ShRUIOp` | `off_59B5F70` | `sub_863380` |
| 37 | `arith.sitofp` | `SIToFPOp` | `off_59B5FC0` | `sub_863DA0` |
| 38 | `arith.subf` | `SubFOp` | `off_59B6010` | `sub_8667D0` |
| 39 | `arith.subi` | `SubIOp` | `off_59B6060` | `sub_866B30` |
| 40 | `arith.truncf` | `TruncFOp` | `off_59B60B0` | `sub_866620` |
| 41 | `arith.trunci` | `TruncIOp` | `off_59B6100` | `sub_865A50` |
| 42 | `arith.uitofp` | `UIToFPOp` | `off_59B6150` | `sub_865C00` |
| 43 | `arith.xori` | `XOrIOp` | `off_59B61A0` | (inlined) |

### The Benefit-20 Specialist for `arith.constant`

`arith.constant` is missing from the table above because it does not route through the generic bank. A hand-written `ConstantTensorOpConversion` at vtable `off_59B5210` registers separately through the parent driver with `PatternBenefit(20)`, pre-empting the generic-fold path for constants. The specialist inspects the constant's `Attribute` payload and synthesises either `nv_tileaa.splat` or `nv_tileaa.constant_tensor` depending on attribute kind — `DenseElementsAttr` versus `SplatElementsAttr` versus `IntegerAttr` versus the rest. That decision logic is shaped differently from the uniform 1:1 elementwise rewrite `GenericOpPattern<Op>` expresses, which is why it lives outside the bank. The benefit-20 value guarantees the dialect-conversion driver prefers the specialist over any default-benefit pattern that might otherwise match a constant op.

### Parent Driver `sub_877280`

The master driver for the whole conversion pass is `sub_877280` (`populate_TileIR_AS_lowering_patterns`, 9 616 B). It builds the `ConversionTarget` marking `llvm`, `cute`, `cute_nvgpu`, `builtin`, and `vector` as fully legal; registers `arith` with a dynamic legality predicate `sub_85E6D0` that returns true once an op has already been rewritten into TileAS form; and marks `nv_tileaa` and `nv_tileas` as legal. It then calls four populate drivers in source order and inlines six hand-written specialists into its own body. The bank covered by this page is the fourth call. Frozen pattern construction runs through `sub_36F9730`, the frozen set goes to `sub_36CB0C0` (`applyPartialConversion`), and a failed partial conversion emits `"expect lower MakeTiledTMADescOp"` through the standard `emitError` path.

| Helper | Address | Role |
|---|---|---|
| `sub_10FEE70` | `0x10FEE70` | Populates `nv_tileaa.block_tile`, `nv_tileaa.make_memref`, `nv_tileaa.get_dim_size` (3 hand-written patterns). |
| `sub_110B1B0` | `0x110B1B0` | Populates `nv_tileaa.bitcast`, `nv_tileaa.ptr_to_int`, `nv_tileaa.int_to_ptr` (3 hand-written patterns). |
| `sub_1A05DA0` | `0x1A05DA0` | Populates the `func` dialect conversions (`FuncOp`, `CallOp`, `ReturnOp`). |
| `sub_873F30` | `0x873F30` | This bank — 43 `GenericOpPattern<arith::*>` instantiations. |
| inline | (in `sub_877280`) | Six hand-written specialists at `0x59B51C0..0x59B5350`: `MakeTiledTMADescOpHostConversion`, `ConstantTensorOpConversion`, `AddPtrOpConversion`, `SplatOpHostConversion`, `AssumeOpConversion`, `ExtractOpHostConversion`. |

## PDL Fallback and the Shared LLVMTypeConverter

Every `Convert*ToLLVM` pass in tileiras leans on two pieces of infrastructure too large to inline into each per-pass description: a PDL-to-PDLInterp fallback that turns embedded PDL bytecode into runnable patterns, and a single shared `LLVMTypeConverter` whose vtable defines the ABI shape for tile, view, token, CuTe, and kernel function types. Both objects are constructed once per pass and threaded through the pattern set; per-pass pages describe only the dialect-specific patterns and refer back here for the plumbing.

### PDL Fallback `sub_36F9730`

`sub_36F9730` (15 119 B) is the frozen-pattern construction step, run immediately before `applyPartialConversion`. It walks every PDL pattern module registered with the active `RewritePatternSet`, compiles each one down to PDL Interpreter ops, and hands the resulting interpreter bodies to the conversion driver alongside the C++ patterns. The PDL compile is what lets TileAS lowering express small rewrite recipes in PDL bytecode instead of yet another bank of CRTP `OpConversionPattern` classes — tileiras's binary embeds the compiled PDL Interpreter bodies in `.rodata`, and the fallback resolves them at apply time, so the on-disk PDL pattern is essentially an interpreter program ready to be wired into the driver's match-and-rewrite loop.

When the PDL compile itself fails — typically because a registered pattern references an op or attribute the interpreter can't resolve in the current dialect registry — `sub_36F9730` emits the literal diagnostic `"failed to lower PDL pattern module to the PDL Interpreter"` and returns failure. The parent driver treats this as a hard pass failure, not a recoverable miss: surviving without the PDL-side patterns would silently change which ops the conversion target deems illegal.

### LLVMTypeConverter Vtable at `0x59dbce0`

The shared `LLVMTypeConverter` carries a 17-slot vtable at `0x59dbce0`. The first 12 slots are the upstream `TypeConverter` base contract — typeinfo helper, two destructors, the conversion entry points, materialization hooks, and the pointer-type helper. The last five slots are tile-specific extensions that handle cute, cute_nvgpu, and nv_tileaa types the LLVM base never sees. Overrides in slots 3, 4, 5, 8, and 9 are where tileiras inserts its own ABI rules: a `convertType` that dispatches into the cute / cute_nvgpu / nv_tileaa table, a `convertCallSignature` that enforces the bare-pointer ABI, a `convertFunctionSignature` that lifts kernel attributes onto the converted function, and the two materialization hooks that produce `builtin.unrealized_conversion_cast` ops at partial-conversion boundaries.

| Slot | Method | Source |
|---|---|---|
| 0 | typeinfo helper | inherited |
| 1 | dtor (delete) | inherited |
| 2 | dtor (no delete) | inherited |
| 3 | `convertType` | overridden — handles cute / cute_nvgpu / nv_tileaa types |
| 4 | `convertCallSignature` | overridden — bare-pointer ABI |
| 5 | `convertFunctionSignature` | overridden — kernel-attribute lift |
| 6 | `convertBlockSignature` | inherited |
| 7 | `convertSignatureArg` | inherited |
| 8 | `materializeSourceConversion` | overridden — emits `builtin.unrealized_conversion_cast` |
| 9 | `materializeTargetConversion` | overridden — emits inverse cast |
| 10 | `materializeArgumentConversion` | inherited |
| 11 | `getPointerType` | inherited |
| 12 | tile-extension: `convertTileType` | new — TileType to llvm.struct |
| 13 | tile-extension: `convertTokenType` | new — TokenType to llvm.token |
| 14 | tile-extension: `convertPipelineIteratorType` | new — PipelineIteratorType to llvm.struct |
| 15 | tile-extension: `convertTensorViewType` | new — TensorViewType to llvm.struct |
| 16 | tile-extension: `convertPartitionViewType` | new — PartitionViewType to llvm.struct |

### Object Layout and Conversion-Callback Bank

The full `LLVMTypeConverter` object is 0xD0 bytes (208 B), allocated as part of pass construction. Layout places the vtable at offset `+0x00`, the base `TypeConverter` internals across `+0x08..+0x80` (reservation buffer, hashed type cache, ordered conversion list, kind-tag word), the addConversion callback array between `+0x80..+0xB0`, the tile-extension state at `+0xB0..+0xC8`, and the `MLIRContext *` at `+0xC8`. Slots 12 through 16 of the vtable read from the tile-extension state cell when dispatching on TileType, TokenType, PipelineIteratorType, TensorViewType, and PartitionViewType. Keeping that cell in the same object as the base internals lets the partial-conversion driver see a single uniform converter, even though tile types and the upstream LLVM types are converted by different machinery.

A contiguous bank at `sub_136EC10..sub_1371130` holds the 25 `addConversion` callbacks, each exactly 424 B and each registered into the ordered conversion list at construction time. Every callback is the per-type rewriter the converter dispatches to when it sees a specific incoming type, and the 25 together cover the full set of cute, cute_nvgpu, nv_tileaa, arith, and scf types the various `Convert*ToLLVM` passes may encounter. Registration order is significant: the converter walks the list in registration order, so a tile-extension callback registered after a base callback sees the type last, and adding callbacks out of order is one of the easier ways to break the bare-pointer ABI on function boundaries.

### Function-Signature Failure Path

`convertFunctionSignature` is the slot most often exercised at pass boundaries because every `func.func` op crossing a dialect boundary runs through it. When it fails — typically because an argument or result type is something neither the base nor any of the 25 tile-extension callbacks recognise — the type converter calls `sub_19DCA80`, which emits the literal diagnostic `"failed to convert function signature type for: "` (verbatim, including the trailing space) followed by the printed form of the offending type. Preserve both the trailing space and the exact wording: downstream regression suites grep for this string, and the trailing space is what separates the message from the appended type so diff-based comparisons stay stable.

### Dispatch Shape

The recovered shape of `convertType` is a straight-line dispatch on incoming type kind. Each arm either calls one of the tile-extension callbacks or falls through to the base `convertType` for the upstream LLVM types. The dispatch makes slots 12 through 16 reachable from the same entry point as the base slots; without the fan-out, the partial-conversion driver would have to know about two different converters — exactly the failure mode the shared converter is designed to prevent.

```c
Type LLVMTypeConverter::convertType(Type t) {
    if (auto tile  = dyn_cast<TileType>(t))           return /*vtable slot 12*/ sub_136EC10(this, tile);
    if (auto tok   = dyn_cast<TokenType>(t))          return /*vtable slot 13*/ sub_136EE40(this, tok);
    /* ... 23 more dispatch arms covering PipelineIteratorType, TensorViewType,        */
    /*     PartitionViewType, and the cute / cute_nvgpu / nv_tileaa / arith / scf      */
    /*     types registered through the addConversion bank ...                          */
    return /*vtable slot 3 base*/ baseConvertType(t);
}
```

Per-pass pages refer back to this section rather than re-documenting the vtable or the addConversion
bank; the only contract a Convert*ToLLVM pass must respect on top of what is described here is that
it must construct exactly one `LLVMTypeConverter` and thread it through every pattern, every
`ConversionTarget` legality predicate, and every PDL pattern module that `sub_36F9730` later compiles.
