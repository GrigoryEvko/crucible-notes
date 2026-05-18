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

The exact roster is maintained in [Operation Roster](op-roster.md#operation-families). Two practical
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

The tile-shape verifier walks the shape, rejecting non-positive or non-power-of-two
dimensions and enforcing a 16-million-element ceiling. The element count is
tracked using a divide-and-compare to detect overflow before it can happen.

```c
LogicalResult verify_tile_shape(ArrayRef<int64_t> shape) {
    const int64_t max_elements = 16 * 1024 * 1024;
    int64_t elements = 1;

    for (int64_t dim : shape) {
        if (dim <= 0) {
            return emit_error("tile dimensions must be positive");
        }
        if ((dim & (dim - 1)) != 0) {
            return emit_error("tile dimensions must be powers of two");
        }
        if (elements > max_elements / dim) {
            return emit_error("tile would exceed the maximum element count");
        }
        elements *= dim;
    }
    return success();
}
```

`tensor_view` uses dynamic shape and stride slots, but each dynamic slot is
still part of a fixed-rank type. The verifier rejects rank mismatches between
shape and stride and rejects any non-positive static dimension or static stride.

```c
LogicalResult verify_tensor_view(Type element_type,
                                 ArrayRef<int64_t> shape,
                                 ArrayRef<int64_t> stride) {
    if (shape.size() != stride.size()) {
        return emit_error("tensor_view shape and stride must have the same rank");
    }
    for (int64_t dim : shape) {
        if (dim != kDynamic && dim <= 0) {
            return emit_error("static tensor_view dimensions must be positive");
        }
    }
    for (int64_t step : stride) {
        if (step != kDynamic && step <= 0) {
            return emit_error("static tensor_view strides must be positive");
        }
    }
    return success();
}
```

`partition_view` is the bridge between logical tensors and tile-shaped access.
The verifier checks rank agreement, validates `dim_map` as an injective function
into tensor axes, enforces the power-of-two tile shape rule, and gates special
padding values on floating-point element types.

```c
LogicalResult verify_partition_view(ArrayRef<int32_t> tile_shape,
                                    TensorViewType tensor,
                                    ArrayRef<int32_t> dim_map,
                                    Optional<PaddingValue> padding) {
    if (tile_shape.empty()) {
        return emit_error("partition tiles must have rank");
    }
    if (tile_shape.size() != tensor.rank()) {
        return emit_error("partition tile rank must match tensor rank");
    }
    if (dim_map.size() != tile_shape.size()) {
        return emit_error("dim_map must cover every tile dimension");
    }

    BitSet used_tensor_dims(tensor.rank());
    for (size_t tile_dim = 0; tile_dim < dim_map.size(); ++tile_dim) {
        if (tile_shape[tile_dim] <= 0) {
            return emit_error("partition tile dimensions must be positive");
        }
        if (!is_power_of_two(tile_shape[tile_dim])) {
            return emit_error("partition tile dimensions must be powers of two");
        }
        int32_t tensor_dim = dim_map[tile_dim];
        if (tensor_dim < 0 || tensor_dim >= (int32_t)tensor.rank()) {
            return emit_error("dim_map target must be inside the tensor rank");
        }
        if (used_tensor_dims.test(tensor_dim)) {
            return emit_error("dim_map must not map two tile dimensions to one tensor dimension");
        }
        used_tensor_dims.set(tensor_dim);
    }

    if (padding.has_value() && padding->is_nan_or_infinity_or_negative_zero()) {
        if (!tensor.element_type().is_float()) {
            return emit_error("special padding values require a floating-point element type");
        }
    }
    return success();
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

The lowering direction is one-way. The driver runs in three phases.

Phase 1: the verifier rejects modules that contain operations from any
non-public dialect. A producer that emits IR through this entry point must
restrict itself to `cuda_tile`, `builtin`, and a small set of supporting
upstream dialects (`arith` constants, `func` symbol references, debug-info
attributes). Any other dialect at this point is a producer bug.

Phase 2: a partial dialect conversion drives the rewrite. The conversion
target marks `cuda_tile` illegal, marks the destination dialects (`arith`,
`math`, `func`, `gpu`, `scf`, `nv_tileaa`) legal, and registers a dynamic
legality check on `ub.poison` so untyped poison values pick up legal
TileAA types as they flow through. Each `cuda_tile` op carries a conversion
pattern that emits the corresponding TileAA shape; the type converter
rewrites scalar, tile, pointer, view, and token types in parallel.

Phase 3: a post-conversion verifier confirms that no `cuda_tile` operation
survived the conversion. After this point, ordinary producers will never see
`cuda_tile` again; the rest of the pipeline works in progressively more
hardware-facing internal dialects (TileAA → TileAS → CuTe → NVGPU → NVVM →
LLVM).

The driver is structured as a single greedy pass rather than a per-family
sweep because the rewrite patterns produce IR that immediately matches further
patterns: a `cuda_tile.load_view_tko` lowers into a TileAA `tiled_load` that
exposes new shape and layout structure to the next op's lowering. A
per-family sweep would force a fixed phase order; the greedy pass lets pattern
match order respond to the IR as the conversion produces it.

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

Every registered op in `cuda_tile` carries one `AbstractOperation` descriptor.
The dialect constructor walks its 92-op roster, allocates one descriptor per
op, fills it from that op's registration thunk, and appends it to the
dialect's registered-op vector. An `Operation*` resolves through its
`OperationName` slot into this descriptor to reach the dialect's interface
tables and fold callback.

The descriptor's logical layout:

| Slot | Purpose |
|---|---|
| op vtable | Per-op dispatch (operand/result accessors, asm-printer hooks). |
| mnemonic | An embedded `StringRef` pointing at a read-only literal in the binary's `.rodata`. |
| inliner interface | Inlining policy for this op. |
| asm interface | Custom asm-printer/parser behavior. |
| fold interface | Operation-fold concept model. |
| type-inference interface | Result-type inference. |
| bytecode interface | Bytecode round-trip. |
| memory-effects interface | Whether the op reads, writes, or allocates memory. |
| destination-style interface | Tensor-style operand/result mapping. |
| extra interface slots | Reserved for future concept models. |
| fold callback | Per-op rewriter that runs during the canonicalize step. |

The descriptor slab is zero-initialized, so unused interface slots stay null and
the dispatcher probes them without a presence flag. The mnemonic field is an
embedded `StringRef` that points at the binary's read-only literal, not a
heap-interned copy — the ASM printer and the verifier read it back verbatim.

The descriptors sit consecutively in a statically-allocated array. The dialect
indexes the array by mnemonic hash through the registration helper documented
in [TypeID Sentinels and Anchors](../../mlir-infra/typeid-sentinels-and-anchors.md#idiom-1-static-pointer-identity-sentinel);
live `Operation*` instances reach the descriptor through their `OperationName`
slot — the resolution path documented in
[Operation Layout — Pointer-Identity Dispatch](../../mlir-infra/operation-layout.md#pointer-identity-dispatch). The
per-op fold-callback assignments for the rest of the roster are catalogued in
[Operation Roster — Op Method Surface](op-roster.md#op-method-surface).

## Cross-links

- [Frontend Contract and Tile IR Emission](../../topics/frontend-contract-and-tile-ir-emission.md) —
  producer-facing rules for kernel signatures, attribute namespaces, operand-
  order conventions, and the bytecode-format constraints a conformant frontend
  must satisfy.
- [Operation Roster](op-roster.md#operation-families) — operation families, producer contract, and
  version-specific mnemonic notes.
- [Types and Attributes](types-and-attrs.md#concrete-types) — public types, element predicates,
  semantic attributes, assumption predicates, and optimization hints.
- [Verifiers](verifiers.md#verification-pipeline) — numeric, memory, region, aggregate, and MMA
  verification contracts.
- [Canonicalizers and Folds](canonicalizers-and-folds.md#fold-surface) — public folds,
  select and if rewrites, and the recursive simplifier contract.
- [Assembly Printer](asm-printer.md#token-ordered-memory-syntax) — textual assembly, token-memory syntax,
  attribute elision, enum spellings, and SSA result-name hints.
