# cuda_tile Dialect Overview

Frontends write `cuda_tile` and the compiler promises to accept it. It is the
public input contract of `tileiras` — the only dialect a producer ever has to
construct — and the gate before lowering descends into the private TileAA,
TileAS, CuTe, CUTLASS, NVGPU, LLVM, and NVVM layers. In practice it is a
compact tile-programming IR: structured control flow, shaped tile values,
view-based memory access, token-threaded side effects, tensor-core operations,
and just enough attributes to preserve numeric and memory semantics until
target-specific lowering takes over.

Producers generate `cuda_tile`; reimplementers treat it as an ABI boundary. A
module that verifies here flows through the rest of the compiler without the
frontend ever touching `nv_tileaa`, `nv_tileas`, or any backend dialect.

## Programming Model

A normal input module is rooted in `cuda_tile.module` and contains one or more
`cuda_tile.entry` operations that each become a GPU kernel. Inside each entry,
the dialect carries its own structured control flow (`if`, `for`, `loop`,
`yield`, `break`, `continue`, `return`) so frontends never have to lower into
`scf` or `func` first.

Values fall into four broad categories:

| Category | Role |
|---|---|
| Tiles | Shaped SSA values with static rank and element type. |
| Views | `ptr`, `tensor_view`, and `partition_view` values that describe memory. |
| Tokens | Ordering edges for memory operations with side effects. |
| Scalars and attributes | Numeric operands, predicates, rounding modes, padding values, and optimization hints. |

The dialect is target-aware but not target-lowered. Accepted element types are
`f16`, `bf16`, `f32`, `tf32`, `f64`, `f8E4M3FN`, `f8E5M2`, and the integer
widths `i1`, `i8`, `i16`, `i32`, `i64`. Architecture-specific choices —
MMA atom selection, TMA materialization, register allocation, FP4/FP6
microscaling, final PTX features — all come later, in the private lowering
pipeline.

## Operation Families

The operation surface is best understood by family rather than by registration
order:

| Family | Examples | Contract |
|---|---|---|
| Arithmetic and logic | `addf`, `addi`, `mulf`, `cmpf`, `cmpi`, `shli`, `xori`, `fma` | Operate on scalar or tile-shaped values while preserving explicit signedness, overflow, comparison, rounding, and fast-math attributes. |
| Math intrinsics | `exp`, `exp2`, `log`, `log2`, `pow`, `rsqrt`, `sin`, `cos`, `sqrt`, `tanh` | Preserve source-level numeric intent until lowered to math, NVVM, or backend intrinsics. |
| Memory and pointers | `load_ptr_tko`, `load_view_tko`, `store_ptr_tko`, `store_view_tko`, `atomic_cas_tko`, `atomic_rmw_tko`, `offset` | Express typed global-memory access and atomics through explicit token dependencies. |
| Structured control flow | `module`, `entry`, `if`, `for`, `loop`, `yield`, `break`, `continue`, `return` | Keep kernel structure and region control flow in the public dialect. |
| Tile shape algebra | `broadcast`, `cat`, `extract`, `permute`, `reshape`, `iota`, `select` | Transform tile shapes and values without choosing hardware layout yet. |
| Reductions and scans | `reduce`, `scan` | Carry reduction dimensions, identities, and pure body regions. |
| MMA | `mmaf`, `mmai` | Describe matrix multiply-accumulate intent before atom selection and schedule generation. |
| Conversion | `exti`, `trunci`, `itof`, `ftoi`, `ftof`, `bitcast`, `int_to_ptr`, `ptr_to_int`, `ptr_to_ptr` | Make type changes explicit so the first lowering pass can preserve legality. |
| Diagnostics and assumptions | `assert`, `assume`, `print`, `constant`, `global`, `get_global` | Preserve compile-time constants, diagnostics, globals, and optimization assumptions. |

The exact roster is maintained in [op-roster.md](op-roster.md). Two practical
version deltas matter for producers targeting this binary: the emitted mnemonic
is `cuda_tile.print`, not the open-source `cuda_tile.print_tko`, and the build
rejects `cuda_tile.atan2` outright.

## Type Contracts

`cuda_tile` types describe the source-level shape and memory model. They should
be treated as verifier-backed contracts, not as backend storage layouts.

| Type | Meaning | Main verifier contract |
|---|---|---|
| `cuda_tile.tile` | Static shaped value with an element type. | Dimensions are positive powers of two; total element count is capped. |
| `cuda_tile.ptr` | Typed global pointer to a numeric scalar element. | Pointee type is numeric; pointer-to-pointer is rejected. |
| `cuda_tile.tensor_view` | Element type plus tensor shape and stride metadata. | Shape and stride ranks match; static dimensions and strides are positive. |
| `cuda_tile.partition_view` | Tile partition over a tensor view. | Tile rank matches tensor rank; `dim_map` covers each tile dimension exactly once; padding is type-compatible. |
| `cuda_tile.token` | Zero-runtime ordering marker. | Used as an SSA dependency for side-effecting operations. |
| `cuda_tile.string` | Observed binary type for string-like handles. | Treat as implementation-specific unless the producer is targeting this exact binary contract. |

The tile-shape verifier is intentionally simple and strong:

```c
bool verify_tile_shape(ArrayRef<int64_t> shape) {
    const int64_t max_elements = 16 * 1024 * 1024;
    int64_t elements = 1;

    for (int64_t dim : shape) {
        require(dim > 0, "tile dimensions must be positive");
        require((dim & (dim - 1)) == 0,
                "tile dimensions must be powers of two");
        require(elements <= max_elements / dim,
                "tile would exceed the maximum element count");
        elements *= dim;
    }

    return true;
}
```

`tensor_view` uses dynamic shape and stride positions, but each dynamic slot is
still part of a fixed-rank type. Static dimensions must remain positive:

```c
bool verify_tensor_view(Type element_type,
                        ArrayRef<int64_t> shape,
                        ArrayRef<int64_t> stride) {
    require(shape.size() == stride.size(),
            "shape and stride must have the same rank");

    for (int64_t dim : shape) {
        require(dim == kDynamic || dim > 0,
                "static tensor dimensions must be positive");
    }

    for (int64_t step : stride) {
        require(step == kDynamic || step > 0,
                "static tensor strides must be positive");
    }

    return true;
}
```

`partition_view` is the bridge between logical tensors and tile-shaped access:

```c
bool verify_partition_view(ArrayRef<int32_t> tile_shape,
                           TensorViewType tensor,
                           ArrayRef<int32_t> dim_map,
                           optional<PaddingValue> padding) {
    require(!tile_shape.empty(), "partition tiles must have rank");
    require(tile_shape.size() == tensor.rank(),
            "tile rank must match tensor rank");
    require(dim_map.size() == tile_shape.size(),
            "dim_map must cover every tile dimension");

    BitSet used_tensor_dims(tensor.rank());
    for (int32_t tile_dim = 0; tile_dim < dim_map.size(); ++tile_dim) {
        require(tile_shape[tile_dim] > 0, "tile dimensions must be positive");
        require(is_power_of_two(tile_shape[tile_dim]),
                "tile dimensions must be powers of two");

        int32_t tensor_dim = dim_map[tile_dim];
        require(0 <= tensor_dim && tensor_dim < tensor.rank(),
                "dim_map target must be inside the tensor rank");
        require(!used_tensor_dims.test(tensor_dim),
                "dim_map must not map two tile dimensions to one tensor dimension");
        used_tensor_dims.set(tensor_dim);
    }

    if (padding && padding->is_nan_or_infinity_or_negative_zero()) {
        require(tensor.element_type().is_float(),
                "special padding values require floating-point element type");
    }

    return true;
}
```

## Memory and Tokens

The `_tko` suffix means token-ordered. Memory effects ride on dataflow: the
token is an SSA value, and a pass may reorder memory operations only when it
preserves the dependency graph that ties them together.

```c
struct Token {};

struct LoadResult {
    Value value;
    Token token;
};

LoadResult load_ptr_tko(Pointer ptr, Indices indices, Token in);
LoadResult load_view_tko(PartitionView view, Indices indices, Token in);

Token store_ptr_tko(Pointer ptr, Indices indices, Value value, Token in);
Token store_view_tko(PartitionView view, Indices indices, Value value, Token in);

struct AtomicResult {
    Value old_or_result;
    Token token;
};

AtomicResult atomic_rmw_tko(Pointer ptr, AtomicOp op, Value value, Token in);
AtomicResult atomic_cas_tko(Pointer ptr, Value expected, Value desired, Token in);
```

A pass may delete, merge, or reorder token-ordered operations only when the
observable token order survives intact. That is the source-level memory
contract that later TileAA and TileAS passes refine into schedulable memory
operations.

## Semantic Attributes

The attribute set is small but consequential. Most attributes are not
decoration — they constrain legal lowering:

| Attribute family | Used by | Meaning |
|---|---|---|
| Comparison predicate/order | `cmpf`, `cmpi`, select-like rewrites | Ordered/unordered floating compares and integer predicate selection. |
| Signedness and overflow | Integer arithmetic, shifts, conversions | Whether integer operations are signed and whether overflow has defined assumptions. |
| Rounding and padding | Floating conversions, partition views | Rounding mode selection and legal fill value for out-of-bounds view reads. |
| Optimization hints | Entries, memory ops, layout-sensitive ops | Producer-supplied scheduling and target hints keyed by architecture or operation kind. |
| Assumption predicates | `assume` and related transforms | Facts such as divisibility, boundedness, and same-elements properties. |
| Debug info | source locations and lexical scopes | Optional provenance carried through lowering when debug/line info is enabled. |

## Key design choice: public because it's the API

`cuda_tile` is public because it is the producer-facing API. Every dialect
below it is an implementation detail. A frontend should construct valid
`cuda_tile`, serialize it as TileIR bytecode, and hand it to `tileiras` —
never touching internal TileAA or TileAS operations.

The lowering direction is one-way:

```c
Module lower_cuda_tile_module(Module module, CompileOptions options) {
    require(module.only_contains_public_input_dialect());
    require(parse_compute_capability(options.compute_capability).ok());

    ConversionTarget target;
    target.add_legal_dialects({"arith", "math", "func", "gpu", "scf", "nv_tileaa"});
    target.add_illegal_dialect("cuda_tile");
    target.add_dynamically_legal_op("ub.poison",
        [&](Operation op) { return type_converter.is_legal(op.result_types()); });

    TypeConverter types;
    types.add(cuda_tile_scalar_to_tileaa_scalar);
    types.add(cuda_tile_tile_to_tileaa_tile);
    types.add(cuda_tile_view_to_tileaa_view);

    RewritePatternSet patterns;
    populate_cuda_tile_to_tileaa_patterns(patterns, types);

    apply_partial_conversion(module, target, patterns);
    require(!module.contains_dialect("cuda_tile"));
    return module;
}
```

After this conversion, ordinary producers will never see `cuda_tile` again.
The rest of the pipeline works in progressively more hardware-facing internal
dialects.

## Open-source cross-reference

The public `cuda_tile` source distribution is the best reference for syntax,
ODS definitions, operation classes, type definitions, and dialect interfaces.
The binary follows that public surface with the practical deltas noted above:
`print_tko` is exposed as `print`, `atan2` is absent, and this binary also
contains an implementation-specific `cuda_tile.string` type.

The useful public source anchors are:

| Area | Public source role |
|---|---|
| Dialect initialization | Registers attributes, types, operations, and dialect interfaces. |
| Operation definitions | TableGen records for the accepted `cuda_tile.*` operation surface. |
| Type definitions | TableGen and C++ verifier/printer code for tile, pointer, tensor view, partition view, and token types. |
| Interfaces | Inlining and asm-printing behavior. |
| Optimizer transforms | Public cleanup transforms that overlap conceptually with, but do not fully describe, the binary's private lowering pipeline. |

## AbstractOperation Record

Every registered op in `cuda_tile` carries a single 0x68-byte `AbstractOperation` record. The dialect ctor walks
its 92-op roster, allocating one record per op via `sub_44A8C20(0x68)`, filling it from that op's reg thunk, and
appending it to the dialect's registered-op vector. An `Operation*` resolves through its `OperationName` slot
into this descriptor to reach the dialect's interface tables and fold callback.

```c
typedef struct AbstractOperation {
    /*+0x00*/ void           **vtable;                       // dispatch for the op
    /*+0x08*/ StringRef        mnemonic;                     // e.g. "cuda_tile.addf"
    /*+0x18*/ ConceptModel    *interface_inliner;            // CudaTileinlinerInterface
    /*+0x20*/ ConceptModel    *interface_opasm;              // CudaTileOpAsmInterface
    /*+0x28*/ ConceptModel    *interface_fold;
    /*+0x30*/ ConceptModel    *interface_typeinfer;
    /*+0x38*/ ConceptModel    *interface_bytecode;
    /*+0x40*/ ConceptModel    *interface_memeffects;
    /*+0x48*/ ConceptModel    *interface_destinationstyle;
    /*+0x50*/ ConceptModel    *interface_extra0;
    /*+0x58*/ ConceptModel    *interface_extra1;
    /*+0x60*/ FoldCallback     fold_canon;                   // op-fold and canonicalize hook
} AbstractOperation;
```

The allocator zero-initializes the slab, so unused interface slots stay null and the dispatcher probes them
without a presence flag. The `mnemonic` field is an embedded `StringRef` pointing at a `.rodata` literal owned
by the binary, not a heap-interned copy: the 9-byte dialect namespace `"cuda_tile"` sits at `0x45e74d0`, and
each op mnemonic literal lives in the same neighbourhood, read back verbatim by the ASM printer and the
verifier. The interface-concept pointers at `+0x18..+0x58` are the MLIR concept-model singletons that wire
inlining, asm printing, folding, type inference, bytecode round-trip, memory effects, and destination-style
behaviour. The fold callback at `+0x60` is the op's per-op rewriter — `cuda_tile.addf`'s reg thunk wires it
to `sub_671150`, and the op's class vtable at `+0x00` is `&unk_59AA120`.

The records sit consecutively in a statically-allocated array in `.data.rel.ro` at `0x5B37F20..0x5B38170`,
which is `92 × 0x68 = 9952` bytes. Three notable anchors inside the bank are `0x5B37F20` for `cuda_tile.return`
(primary), `0x5B37FA8` for its secondary interface slot, `0x5B380C0` for `cuda_tile.if`, and `0x5B38170` for
`cuda_tile.continue`. The end-of-registered-ops boundary is marked by the null sentinel at `0x5BE6138`; lookup
helpers stop walking the bank when they hit it.

This is the static-sentinel idiom described in
[mlir-infra/typeid-sentinels-and-anchors.md](../../mlir-infra/typeid-sentinels-and-anchors.md): the bank is
allocated once, lives for the entire process, and is indexed by mnemonic hash through the small dispatch
table in `sub_5F8DC0`. Live `Operation*` instances reach this record through their `OperationName` slot —
the resolution path documented in
[mlir-infra/operation-layout.md](../../mlir-infra/operation-layout.md). The per-op `vtable` and
fold-callback pairs for the rest of the roster are catalogued in [op-roster.md](op-roster.md).

## Cross-links

- [op-roster.md](op-roster.md) — operation families, producer contract, and
  version-specific mnemonic notes.
- [types-and-attrs.md](types-and-attrs.md) — public types, element predicates,
  semantic attributes, assumption predicates, and optimization hints.
- [verifiers.md](verifiers.md) — numeric, memory, region, aggregate, and MMA
  verification contracts.
- [canonicalizers-and-folds.md](canonicalizers-and-folds.md) — public folds,
  select and if rewrites, and the recursive simplifier contract.
- [asm-printer.md](asm-printer.md) — textual assembly, token-memory syntax,
  attribute elision, enum spellings, and SSA result-name hints.
