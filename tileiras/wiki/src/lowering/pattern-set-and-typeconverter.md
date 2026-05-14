# Pattern Sets and Type Conversion

## Abstract

Tileiras lowering rides on ordinary MLIR dialect conversion: a conversion target declares what is legal, a type converter defines ABI shape, and a rewrite pattern set rewrites illegal operations until the target accepts the module. The public contract is short — every lowering stage must build a complete pattern set, and one coherent LLVM type converter must span TileAA, TileAS, CuTe, NVGPU, and kernel function boundaries. Two passes that disagree about descriptor shape or address-space numbering produce a module that verifies but generates wrong PTX.

This page is the deep reference for that contract: the kinds of pattern objects each stage installs, the type-conversion rules every stage shares, the runtime descriptor shapes later passes assume, and the two anchor patterns — the 43-instantiation arith bank and the kernel-attribute-aware shared LLVM type converter — that the per-stage pages refer back to instead of re-documenting in place.

## Pattern Categories

Each lowering stage installs patterns from one of four categories. The choice depends on whether the source op rewrites to a single destination op, a small fixed sequence, or a region-rewriting transform.

| Category | When to use |
|---|---|
| Generic one-to-one (`GenericOpPattern<SourceOp>`) | Source op and target op are semantically equivalent; converter handles operand types and the rewrite is a same-name, different-dialect copy. The arith bank below is the canonical example. |
| Dedicated `OpConversionPattern<SourceOp>` | Region surgery, witness-attribute reads (CopyAtom, ReduceAtom), compute-capability gates, descriptor packing, inline assembly, or any rewrite that emits more than one target op. |
| One-to-N pattern | Async-pipeline operations that produce multiple SSA values with distinct LLVM types and need `applyPartialOneToNConversion` rather than the standard partial-conversion engine. |
| Cleanup pattern | Runs after the dialect boundary has moved. Removes `builtin.unrealized_conversion_cast`, folds residual `arith` and `math` ops the dialect-conversion target has now made legal. |

The dialect-conversion engine treats all four categories identically — they differ only in what they do inside `matchAndRewrite`.

## Shared LLVM Type Converter

The Tileiras LLVM type converter is an ABI object. Its job is to map every type the lowering stages produce — TileAA memref, TileAS tiled view, async/memory/pipeline tokens, CuTe layout and atom types, function signatures, and the upstream LLVM types — to a single canonical LLVM dialect representation. The dispatcher walks an ordered list of `addConversion` callbacks and returns the first match.

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

Function-signature conversion is the one place the converter must do more than per-type translation: the bare-pointer kernel ABI demands that ranked memref arguments lower to a single aligned pointer plus separately carried launch metadata, not the full descriptor. When `convertFunctionSignature` fails, the converter emits `"failed to convert function signature type for: "` (the trailing space is preserved verbatim) followed by the printed form of the offending type. Downstream regression suites grep for this string; the wording is fixed.

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

Partial conversion sometimes needs a bridge value while only part of the IR has been lowered. The converter installs source and target materialization hooks that both produce `builtin.unrealized_conversion_cast`. The cast-reconciliation phase (phase 8 of `ConvertTileASToLLVM`) erases them once every participating operation has converted. Bridges that survive the final cleanup pass must fail the module rather than reach LLVM translation.

## Pattern Discipline

Generic one-to-one patterns must not inspect target hardware — they exist to be benefit-1 fallbacks the engine prefers only when no specialist matches. Dedicated patterns own every target-feature check they introduce, so the legality of an SM100-only rewrite is visible at the point where the rewrite happens. Region-rewriting patterns convert block-argument types and terminators in the same step, or the resulting parent op fails verification with a signature that no longer matches its region. One-to-N async-pipeline patterns run only after scheduling and layout assignment have made the pipeline structure explicit; running them earlier would split tokens before the scheduler can reason about them. Cleanup patterns leave memory-ordering operations alone unless the op is outside the memory-consistency interface.

## The 43-Instantiation Arith Bank

The TileAA-to-TileAS arith populator installs 43 instantiations of a single CRTP template, one per `arith` op that has a same-named TileAS counterpart. Every instantiation derives from `OpConversionPattern<SourceOp>` and overrides `matchAndRewrite` to do the generic same-name remap.

```cpp
template <typename SourceOp>
class GenericOpPattern : public mlir::OpConversionPattern<SourceOp> {
 public:
  using mlir::OpConversionPattern<SourceOp>::OpConversionPattern;
  using OpAdaptor = typename SourceOp::Adaptor;
  LogicalResult matchAndRewrite(SourceOp op, OpAdaptor adaptor,
                                ConversionPatternRewriter &rw) const override;
};
```

Each rewrite has the shape:

```mlir
%r = arith.addf %a, %b : tensor<8x64xf32>
   ↓
%r = nv_tileas.addf %a, %b : tensor<8x64xf32>
```

The pattern reads operand types through the shared type converter, builds the destination op with the converted operands, copies semantic attributes (rounding mode, fast-math flags, predicate kind, overflow flags), and replaces the source. Any failure in operand or result-type conversion bubbles up as a pattern failure rather than producing a partial replacement.

### Op-Mnemonic Roster

The 43 mnemonics, in registration order. The order matches the source-level `patterns.add<...>(typeConverter, context)` argument list and is reproducible by walking the populator's body in linker-emission order.

| # | Mnemonic | Op class | # | Mnemonic | Op class |
|---:|---|---|---:|---|---|
| 1 | `arith.cmpf` | `CmpFOp` | 23 | `arith.minnumf` | `MinNumFOp` |
| 2 | `arith.cmpi` | `CmpIOp` | 24 | `arith.minsi` | `MinSIOp` |
| 3 | `arith.addf` | `AddFOp` | 25 | `arith.minui` | `MinUIOp` |
| 4 | `arith.addi` | `AddIOp` | 26 | `arith.mulf` | `MulFOp` |
| 5 | `arith.andi` | `AndIOp` | 27 | `arith.muli` | `MulIOp` |
| 6 | `arith.bitcast` | `BitcastOp` | 28 | `arith.negf` | `NegFOp` |
| 7 | `arith.ceildivsi` | `CeilDivSIOp` | 29 | `arith.ori` | `OrIOp` |
| 8 | `arith.ceildivui` | `CeilDivUIOp` | 30 | `arith.remf` | `RemFOp` |
| 9 | `arith.divf` | `DivFOp` | 31 | `arith.remsi` | `RemSIOp` |
| 10 | `arith.divsi` | `DivSIOp` | 32 | `arith.remui` | `RemUIOp` |
| 11 | `arith.divui` | `DivUIOp` | 33 | `arith.select` | `SelectOp` |
| 12 | `arith.extf` | `ExtFOp` | 34 | `arith.shli` | `ShLIOp` |
| 13 | `arith.extsi` | `ExtSIOp` | 35 | `arith.shrsi` | `ShRSIOp` |
| 14 | `arith.extui` | `ExtUIOp` | 36 | `arith.shrui` | `ShRUIOp` |
| 15 | `arith.floordivsi` | `FloorDivSIOp` | 37 | `arith.sitofp` | `SIToFPOp` |
| 16 | `arith.fptosi` | `FPToSIOp` | 38 | `arith.subf` | `SubFOp` |
| 17 | `arith.fptoui` | `FPToUIOp` | 39 | `arith.subi` | `SubIOp` |
| 18 | `arith.maximumf` | `MaximumFOp` | 40 | `arith.truncf` | `TruncFOp` |
| 19 | `arith.maxnumf` | `MaxNumFOp` | 41 | `arith.trunci` | `TruncIOp` |
| 20 | `arith.maxsi` | `MaxSIOp` | 42 | `arith.uitofp` | `UIToFPOp` |
| 21 | `arith.maxui` | `MaxUIOp` | 43 | `arith.xori` | `XOrIOp` |
| 22 | `arith.minimumf` | `MinimumFOp` |   |   |   |

### The Benefit-20 Specialist for `arith.constant`

`arith.constant` is absent from the bank because it does not route through the generic same-name remap. A hand-written `ConstantTensorOpConversion` registers separately with `PatternBenefit(20)`, which pre-empts any default-benefit pattern that might otherwise match a constant op. The specialist inspects the constant's `Attribute` payload and synthesises one of three destinations:

```mlir
arith.constant dense<1.0> : tensor<8x64xf32>          // SplatElementsAttr branch
   ↓
%r = nv_tileaa.splat 1.0 : tensor<8x64xf32>
```

```mlir
arith.constant dense<[1.0, 2.0, ...]> : tensor<32xf32>   // DenseElementsAttr branch
   ↓
%r = nv_tileaa.constant_tensor dense<[1.0, 2.0, ...]> : tensor<32xf32>
```

Scalar `IntegerAttr` and `FloatAttr` payloads keep the same `arith.constant` form, since arith constants are dynamically legal in the target dialect at scalar types. The decision is structural, not numeric — the specialist branches on attribute kind, not on attribute value.

### Parent Driver

The TileAA-to-TileAS pattern bank composes four sub-populators plus six hand-written specialists inlined into the parent body. Sub-populator order is fixed.

| Sub-populator | Patterns |
|---|---|
| `nv_tileaa` structure | `nv_tileaa.block_tile`, `nv_tileaa.make_memref`, `nv_tileaa.get_dim_size` |
| `nv_tileaa` bitcast/ptr | `nv_tileaa.bitcast`, `nv_tileaa.ptr_to_int`, `nv_tileaa.int_to_ptr` |
| `func` dialect | `func.func`, `func.call`, `func.return` |
| arith generic bank | the 43 `GenericOpPattern<arith::*>` instantiations above |
| inline specialists | `MakeTiledTMADescOpHostConversion`, `ConstantTensorOpConversion`, `AddPtrOpConversion`, `SplatOpHostConversion`, `AssumeOpConversion`, `ExtractOpHostConversion` |

The parent driver builds the `ConversionTarget` marking `llvm`, `cute`, `cute_nvgpu`, `builtin`, and `vector` as fully legal; registers `arith` with a dynamic legality predicate that returns true once an op already has TileAS-form operands; and marks `nv_tileaa` and `nv_tileas` as legal. A failed partial conversion emits the diagnostic `"expect lower MakeTiledTMADescOp"`.

## PDL Fallback

Every `Convert*ToLLVM` pass runs a PDL-to-PDLInterp fallback immediately before `applyPartialConversion`. The fallback walks the PDL pattern modules registered with the active `RewritePatternSet`, compiles each module down to PDL Interpreter operations, and hands the resulting interpreter bodies to the conversion driver alongside the C++ patterns. The compiled PDL Interpreter bodies live as fixed bytecode in the binary's read-only data; the fallback's job is to materialize them as runnable patterns at pass time, so the on-disk PDL pattern is essentially an interpreter program ready to be wired into the match-and-rewrite loop.

When PDL compilation fails — typically because a registered pattern references an op or attribute the interpreter cannot resolve in the current dialect registry — the fallback emits `"failed to lower PDL pattern module to the PDL Interpreter"` and returns failure. The parent driver treats this as a hard pass failure rather than a recoverable miss: surviving without the PDL-side patterns would silently change which ops the conversion target deems illegal, producing a module that compiles but mishandles the pattern's intended rewrite.

## Shared LLVMTypeConverter Contract

The shared `LLVMTypeConverter` extends the upstream MLIR `TypeConverter` with the five tile-extension hooks Tileiras needs and three overridden methods that enforce the kernel-ABI rules above.

| Method | Status | Role |
|---|---|---|
| `convertType` | overridden | Dispatches to the tile-extension callbacks first, then falls through to the upstream `LLVMTypeConverter` base for ordinary LLVM types. |
| `convertCallSignature` | overridden | Enforces the bare-pointer ABI for kernel call sites: ranked memref arguments become aligned pointers plus separately-carried launch metadata. |
| `convertFunctionSignature` | overridden | Lifts kernel-spec fields onto the converted function and emits `"failed to convert function signature type for: "` (trailing space preserved) when a type is unrecognised. |
| `materializeSourceConversion` | overridden | Emits `builtin.unrealized_conversion_cast` as the source-side bridge during partial conversion. |
| `materializeTargetConversion` | overridden | Emits the inverse `builtin.unrealized_conversion_cast` for the target side. |
| `convertTileType` | tile-extension | Maps `TileType` to a `llvm.struct` payload. |
| `convertTokenType` | tile-extension | Maps `TokenType` to `i32` for memory/async/pipeline tokens. |
| `convertPipelineIteratorType` | tile-extension | Maps `PipelineIteratorType` to a small `llvm.struct` carrying stage and phase. |
| `convertTensorViewType` | tile-extension | Maps `TensorViewType` to the tiled-view descriptor (base pointer plus packed metadata). |
| `convertPartitionViewType` | tile-extension | Maps `PartitionViewType` to a partition descriptor. |

`convertType` walks an ordered list of `addConversion` callbacks; the first callback that recognises an incoming type wins. Tile-extension callbacks register before the base LLVM callbacks so the partial-conversion driver sees a single uniform converter rather than fanning out across two different machines for tile-only versus upstream-LLVM types.

```c
Type LLVMTypeConverter::convertType(Type t) {
    if (auto tile     = dyn_cast<TileType>(t))              return convertTileType(tile);
    if (auto tok      = dyn_cast<TokenType>(t))             return convertTokenType(tok);
    if (auto pipeIt   = dyn_cast<PipelineIteratorType>(t))  return convertPipelineIteratorType(pipeIt);
    if (auto view     = dyn_cast<TensorViewType>(t))        return convertTensorViewType(view);
    if (auto part     = dyn_cast<PartitionViewType>(t))     return convertPartitionViewType(part);
    /* ... callbacks for cute / cute_nvgpu / nv_tileaa / arith / scf types ...        */
    return baseLLVMConvertType(t);
}
```

A `Convert*ToLLVM` pass must construct exactly one `LLVMTypeConverter` and thread it through every pattern, every `ConversionTarget` legality predicate, and every PDL pattern module the fallback later compiles. Constructing two converters in the same pass — for example, one for body patterns and one for cleanup — is the most common way to silently break the bare-pointer ABI on function boundaries, because the two converters disagree on whether a ranked memref kernel argument is a pointer or a descriptor.

## Cross-References

[Overview](overview.md) shows where each pass that uses this infrastructure sits in the cascade. [cuda_tile to TileAA](cuda-tile-to-tileaa.md), [TileAA to TileAS](tileaa-to-tileas.md), [TileAS to LLVM](tileas-to-llvm.md), [CuTe and CuTe-NVGPU to LLVM](cute-and-cute_nvgpu-to-llvm.md), and [nvgpu / gpu to NVVM](nvgpu-and-gpu-to-nvvm.md) are the five passes that install patterns through the bank and thread the shared type converter described here.
