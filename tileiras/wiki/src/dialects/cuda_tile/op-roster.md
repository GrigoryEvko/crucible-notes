# cuda_tile Operation Roster

## Abstract

A frontend emitting `cuda_tile` is writing tile values, structured kernel
control flow, token-ordered memory effects, tensor views, matrix
multiply-accumulate intent, and source-level numeric attributes — everything
the compiler will subsequently lower into private implementation dialects.
This page is the producer and reimplementation reference: operation families,
the behavior each family promises, and how a compiler should lower the surface
without leaning on internal registration details.

In this build, the token-ordered print operation is spelled `cuda_tile.print`.
The newer `cuda_tile.atan2` is rejected outright, so a frontend that supports
multiple TileIR revisions should gate it behind explicit version logic.

## Operation Families

| Family | Operations | Contract |
|---|---|---|
| Floating and integer arithmetic | `absf`, `absi`, `addf`, `addi`, `ceil`, `cmpf`, `cmpi`, `cos`, `cosh`, `divf`, `divi`, `exp`, `exp2`, `floor`, `fma`, `log`, `log2`, `maxf`, `maxi`, `minf`, `mini`, `mulf`, `mulhii`, `muli`, `negf`, `negi`, `pow`, `remf`, `remi`, `rsqrt`, `sin`, `sinh`, `sqrt`, `subf`, `subi`, `tan`, `tanh` | Operate elementwise on scalar or tile values while preserving rounding, signedness, overflow, comparison, and fast-math choices. |
| Integer logic | `andi`, `ori`, `shli`, `shri`, `xori` | Bitwise and shift operations over integer scalar or tile values. |
| Token-ordered memory | `load_ptr_tko`, `load_view_tko`, `store_ptr_tko`, `store_view_tko`, `atomic_cas_tko`, `atomic_rmw_tko`, `make_token`, `join_tokens`, `offset`, `global`, `get_global`, `make_tensor_view`, `make_partition_view` | Express pointer, view, global, token, and atomic memory behavior without committing to backend layout or scheduling. |
| Structured control flow | `module`, `entry`, `if`, `for`, `loop`, `yield`, `break`, `continue`, `return`, `assert`, `assume` | Keep kernel structure in the source dialect and verify region arity, yielded values, and early-exit ancestry. |
| Shape algebra | `broadcast`, `cat`, `extract`, `get_index_space_shape`, `get_num_tile_blocks`, `get_tensor_shape`, `get_tile_block_id`, `iota`, `permute`, `reshape` | Transform tile rank, tile extents, launch geometry, and indexing without choosing hardware layout. |
| Reductions and scans | `reduce`, `scan` | Carry the reduction dimension, identities, input/result types, and pure combiner body. |
| Matrix multiply-accumulate | `mmaf`, `mmai` | Preserve floating and integer MMA intent until atom selection and scheduler lowering. |
| Type conversion | `bitcast`, `exti`, `ftof`, `ftoi`, `int_to_ptr`, `itof`, `ptr_to_int`, `ptr_to_ptr`, `trunci` | Make widening, narrowing, bit reinterpretation, float/int conversion, and pointer casts explicit. |
| Constants, selection, diagnostics | `constant`, `select`, `print` | Materialize literal values, value selection, and token-ordered runtime diagnostics. |

The family boundaries are semantic, not syntactic. `fma` is arithmetic because
it is elementwise; `mmaf` and `mmai` are MMA because they contract matrix
dimensions. `assert` and `assume` live with control flow because regions and
dominance scope their meaning, even though their payload is an attribute or
predicate.

## Producer Contract

A valid producer should build modules with this shape:

```mlir
cuda_tile.module {
    cuda_tile.entry @kernel(%arg0 : !cuda_tile.tensor_view<...>) {
        %tok0 = cuda_tile.make_token : !cuda_tile.token
        %tile, %tok1 = cuda_tile.load_view_tko %view[%i, %j] token=%tok0
        %acc = cuda_tile.mmaf %a, %b, %c : ...
        %tok2 = cuda_tile.store_view_tko %view[%i, %j], %acc token=%tok1
        cuda_tile.return
    }
}
```

The exact textual syntax is described in [Assembly Printer](asm-printer.md#token-ordered-memory-syntax), but
the contract is independent of formatting:

- memory effects are threaded through `cuda_tile.token`;
- tile values have static rank and element type;
- view values carry shape and stride metadata;
- structured control flow yields values rather than branching through `cf`;
- numeric choices such as rounding and signedness are attributes, not implicit
  frontend assumptions;
- debug info and optimization hints may be present but must not be required for
  semantic correctness.

## Lowering Sketch

The first lowering stage converts public `cuda_tile` into alias-aware TileAA.
Arithmetic and shape operations keep their mathematical meaning intact. Memory
operations gain explicit memref and token structure. Control flow is rewritten
only once region and token legality are already proven.

```c
Module lower_cuda_tile_to_tileaa(Module module, Target target) {
    require(module.only_uses_dialect("cuda_tile", "builtin", "arith"));
    verify_cuda_tile_module(module, target);

    TypeConverter types;
    types.add(convert_scalar_type);
    types.add(convert_tile_type);
    types.add(convert_pointer_type);
    types.add(convert_view_type);
    types.add(convert_token_type);

    RewritePatternSet patterns;
    add_arithmetic_patterns(patterns, types);
    add_shape_patterns(patterns, types);
    add_memory_patterns(patterns, types);
    add_control_flow_patterns(patterns, types);
    add_mma_patterns(patterns, types);

    apply_conversion(module, patterns);
    require(!module.contains_dialect("cuda_tile"));
    return module;
}
```

Lowering must not erase source-level facts prematurely. A `load_view_tko`
becomes an operation with explicit view, index, mask, fallback, memory
ordering, memory scope, and token dependencies — not an unstructured pointer
load until the alias and layout passes have the context to handle it safely.

## Numeric Operations

Arithmetic ops accept scalar or tile-shaped operands. Tile operands must agree
on shape and element type unless the op has an explicit shape-changing
contract. Floating operations carry rounding mode and flush-to-zero policy
forward until a lower dialect decides whether the target instruction can encode
those choices directly.

```c
Value lower_elementwise_arith(ArithOp op) {
    require_same_shape(op.operands);
    require_legal_element_type(op);

    NumericPolicy policy = {
        .rounding = op.rounding_mode,
        .flush_to_zero = op.flush_to_zero,
        .signedness = op.signedness,
        .overflow = op.overflow,
    };

    return tileaa_elementwise(op.kind, op.operands, policy);
}
```

`mulhii` returns the high half of a signed integer product. Implement it as a
wide multiply followed by a high-half extract — never as ordinary
multiplication that relies on target-width overflow.

## Operand and Result Tables

The most heavily emitted ops carry the following operand/attribute/result
shape. The `_tko` family threads a `cuda_tile.token` through every memory
effect.

### `cuda_tile.load_view_tko`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | view | `partition_view` | yes | source tile view |
| operand 1..R | indices | `index` | yes (R = tile rank) | per-axis tile coordinate |
| operand R+1 | mask | `tile<S × i1>` | optional | per-lane predicate |
| operand R+2 | other | `tile<S × element>` | optional | fallback value when masked off |
| operand R+3 | token | `cuda_tile.token` | yes | input ordering edge |
| result 0 | value | `tile<S × element>` | yes | matches view element type |
| result 1 | token | `cuda_tile.token` | yes | successor ordering edge |
| attr `mem_semantic` | enum | `weak\|relaxed\|acquire` | optional | acquire requires scope |
| attr `mem_scope` | enum | `tl_blk\|cluster\|gpu\|sys` | conditional | required for non-weak |
| attr `optimization_hints` | dict | architecture-keyed | optional | |
| attr `operandSegmentSizes` | dense i32 | length 5 | yes | `{view, indices, mask, other, token}` |

A representative two-dimensional tile load with a predicate mask:

```mlir
%tile, %t1 = cuda_tile.load_view_tko %view[%i, %j], %mask, %fallback, %t0
    { mem_semantic = #cuda_tile<mem_semantic relaxed>,
      mem_scope    = #cuda_tile<mem_scope gpu>,
      operandSegmentSizes = array<i32: 1, 2, 1, 1, 1> }
    : !cuda_tile.partition_view<128x64xf32>, index, index,
      tile<128x64xi1>, tile<128x64xf32>, !cuda_tile.token
    -> tile<128x64xf32>, !cuda_tile.token
```

The mask and fallback shapes equal the result tile shape; the view element type matches the result element type. The token chain threads `%t0` in and `%t1` out, ordering the load against any preceding or following memory effect that consumes the same chain.

### `cuda_tile.store_view_tko`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | view | `partition_view` | yes | destination view |
| operand 1 | value | `tile<S × element>` | yes | element type matches view |
| operand 2..R+1 | indices | `index` | yes | per-axis tile coordinate |
| operand R+2 | mask | `tile<S × i1>` | optional | |
| operand R+3 | token | `cuda_tile.token` | yes | input ordering edge |
| result 0 | token | `cuda_tile.token` | yes | successor ordering edge |
| attr `mem_semantic` | enum | `weak\|relaxed\|release` | optional | acquire variants rejected |
| attr `mem_scope` | enum | as above | conditional | |
| attr `operandSegmentSizes` | dense i32 | length 5 | yes | |

### `cuda_tile.atomic_rmw_tko`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | pointers | `tile<S × ptr>` | yes | per-lane address |
| operand 1 | value | `tile<S × element>` | yes | RMW operand |
| operand 2 | mask | `tile<S × i1>` | optional | |
| operand 3 | token | `cuda_tile.token` | yes | |
| result 0 | old | `tile<S × element>` | yes | |
| result 1 | token | `cuda_tile.token` | yes | |
| attr `kind` | enum | `add\|addf\|and\|or\|xor\|xchg\|min\|max\|umin\|umax` | yes | |
| attr `ordering` | enum | full | yes | |
| attr `scope` | enum | full | conditional | |

### `cuda_tile.mmaf` / `cuda_tile.mmai`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | A | `tile<[B ×] M × K × elem_a>` | yes | rank 2 or 3 (batched) |
| operand 1 | B | `tile<[B ×] K × N × elem_b>` | yes | K agrees with A |
| operand 2 | C | `tile<[B ×] M × N × elem_c>` | yes | accumulator |
| result 0 | D | `tile<[B ×] M × N × elem_c>` | yes | shape equals C shape |
| attr `signedness_a` | enum | `signed\|unsigned` | integer MMA | required for `mmai` |
| attr `signedness_b` | enum | `signed\|unsigned` | integer MMA | required for `mmai` |
| attr `rounding` | enum | IEEE basic | optional | `mmaf` only |

A 16×16×16 floating MMA with an f32 accumulator and f16 inputs:

```mlir
%d = cuda_tile.mmaf %a, %b, %c
    : tile<16x16xf16>, tile<16x16xf16>, tile<16x16xf32>
    -> tile<16x16xf32>
```

A batched integer MMA with explicit signedness attributes:

```mlir
%d = cuda_tile.mmai %a, %b, %c
    { signedness_a = #cuda_tile<signedness signed>,
      signedness_b = #cuda_tile<signedness unsigned> }
    : tile<4x16x32xi8>, tile<4x32x16xi8>, tile<4x16x16xi32>
    -> tile<4x16x16xi32>
```

The M/N dimensions of A and B agree with C; the K dimension is contracted. The verifier rejects rank mismatch, K disagreement, accumulator/result type mismatch, missing signedness on `mmai`, and any input/accumulator pair that lies outside the target's legal MMA element-type tuple.

### `cuda_tile.if`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | condition | `i1` | yes | scalar predicate |
| region 0 | then | terminated by `yield` | yes | yields result_types |
| region 1 | else | terminated by `yield` | required when results non-empty | yields result_types |
| result 0.. | values | any non-view type | optional | view-typed results rejected |

### `cuda_tile.for`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | lower | integer | yes | |
| operand 1 | upper | integer | yes | same width as lower |
| operand 2 | step | integer | yes | same width as lower |
| operand 3.. | iter args | any non-view | optional | types equal result_types |
| region 0 | body | terminated by `yield` | yes | block arg 0 = induction var |
| result 0.. | yielded iter args | any non-view | optional | |

## Memory and Token Operations

The `_tko` suffix means token ordered. Every token-ordered memory op consumes
an input token and produces a successor. Loads and atomics also produce data;
stores produce only the successor token. That discipline is the public memory
model — later passes refine it into barriers, async copies, and backend memory
instructions.

```c
LoadResult lower_load_ptr_tko(LoadPtrTkoOp op) {
    MemRef ref = make_memref_from_pointer(op.pointer, op.indices);
    MemoryPolicy policy = memory_policy(op.ordering, op.scope, op.hints);

    Value data = tileaa_load(ref, op.mask, op.padding, policy, op.input_token);
    Token next = token_after(data.memory_effect, op.input_token);
    return (LoadResult){ .value = data, .token = next };
}
```

Atomics check both memory ordering and element type. Integer bitwise modes
are integer-only; floating add is floating-only; compare-and-swap is restricted
to element widths the backend can update atomically.

## Structured Control Flow

`cuda_tile` ships its own region operations because frontends need a stable
kernel-level API. Later lowering may translate these regions into SCF, CFG, or
private control-flow dialects, but the verifier enforces these rules first:

- `if` result types match every non-empty yielding branch;
- `for` induction, bounds, step, iter args, and results are type-consistent;
- `loop` iter args and results are type-consistent;
- `break` exits the nearest compatible `loop`;
- `continue` exits to the next iteration of a compatible `for` or `loop`;
- `return` appears in an `entry` context and matches the entry function type;
- `yield` appears only in a parent op that expects region yields.

## MMA Operations

`mmaf` and `mmai` are deliberately narrow public abstractions: they describe
matrix multiply-accumulate intent, not final tensor-core instruction selection.
The verifier checks shape compatibility and element-type legality. Choosing
[WGMMA](../../topics/wgmma-emission-protocol.md), smaller MMA atoms,
[tensor-memory paths](../../topics/tcgen05-tensor-memory-model.md), or emulation is left to the
lowering pipeline.

```c
LogicalResult verify_mma_shape(Tile lhs, Tile rhs, Tile acc, Tile result) {
    require(lhs.rank == 2 || lhs.rank == 3);
    require(rhs.rank == lhs.rank);
    require(acc.rank == lhs.rank);
    require(result.rank == lhs.rank);

    if (lhs.rank == 3) {
        require(lhs.dim(0) == rhs.dim(0));
        require(lhs.dim(0) == acc.dim(0));
        require(lhs.dim(0) == result.dim(0));
    }

    require(lhs.k_dim == rhs.k_dim);
    require(lhs.m_dim == acc.m_dim);
    require(rhs.n_dim == acc.n_dim);
    require(acc.shape == result.shape);
    return success();
}
```

## Version Notes

- Emit `cuda_tile.print` for runtime diagnostic printing in this build.
- Do not emit `cuda_tile.print_tko` unless targeting a source tree that uses
  that mnemonic.
- Do not emit `cuda_tile.atan2` for this build; guard it behind a newer TileIR
  version check.
- Treat `cuda_tile.string` as implementation-specific unless the target
  contract explicitly documents it.

## Op Method Surface

Every `cuda_tile.*` op exposes four registered functions to the framework:
a builder (or textual parse entry), a registration thunk that interns the
mnemonic and installs the op vtable, a verifier hook, and a lowering pattern
that rewrites the op during the first conversion stage. The functions follow
predictable shapes by op family.

| Family | Builder shape | Verifier shape | Lowering arm |
|---|---|---|---|
| Trivial unary (`absf`, `absi`, `ceil`, `floor`, `negf`, `negi`, `sqrt`, `cos`, `sin`, transcendentals) | Default trampoline; constructs result from one operand and forwards rounding/flush-to-zero attributes. | Generic trait-only verification with element-type and rank checks. | Arithmetic-group conversion pattern. |
| Floating binary (`addf`, `subf`, `mulf`, `divf`, `maxf`, `minf`, `remf`) | Forwards rounding mode and flush-to-zero. | Type-equality, shape-equality, and rounding-mode legality (see [Verifiers — Type-Compatibility Diagnostics](verifiers.md#type-compatibility-diagnostics)). | Arithmetic-group conversion pattern. |
| Integer binary (`addi`, `subi`, `muli`, `divi`, `maxi`, `mini`, `mulhii`, `remi`, `andi`, `ori`, `xori`, `shli`, `shri`) | Forwards signedness and overflow attributes. | Type-equality, shape-equality, signedness-presence. | Arithmetic-group conversion pattern (integer max routes through a dedicated arm). |
| Conversion (`exti`, `trunci`, `ftof`, `ftoi`, `itof`, `bitcast`, `int_to_ptr`, `ptr_to_int`, `ptr_to_ptr`) | Builds result from operand element type and target element type. | Width-direction and rounding-mode checks; identity conversions are rejected. | Pointer-cast specialty arm for the four pointer-family ops; arithmetic-group arm for the rest. |
| Shape (`broadcast`, `cat`, `extract`, `permute`, `reshape`, `iota`) | Builds result from result shape, source shape, and axis attributes. | Rank, element-count, and axis legality. | Arithmetic-group conversion pattern. |
| Token-ordered memory (`load_*_tko`, `store_*_tko`, `atomic_*_tko`, `make_token`, `join_tokens`) | Builds the result tile plus the successor token; threads the input token through. | Token presence, pointer/value element-type match, mask shape match, ordering/scope pairing. | Arithmetic-group arm; the lowering produces a TileAA `tiled_load`/`tiled_store`/`atomic_rmw` and threads the new mem-token chain. |
| View construction (`make_tensor_view`, `make_partition_view`, `offset`, `global`, `get_global`) | Builds the view type from element type, shape, stride, and dynamic operands. | Dynamic-operand count match, element-type compatibility, partition `dim_map` injectivity. | Arithmetic-group arm; lowers to TileAA `make_memref` plus address-space metadata. |
| Structured control flow (`module`, `entry`, `if`, `for`, `loop`, `yield`, `break`, `continue`, `return`) | Builds region(s) plus block argument types from result types and iter-arg types. | Region structure, terminator arity, yield-type match, view-result rejection. | Routes through the control-flow conversion arm that produces SCF/CF dialect output. |
| Aggregate (`reduce`, `scan`) | Builds the result-type list plus the combiner body region. | Body purity, rank-zero block argument types, identity-vs-input element-type match. | Arithmetic-group arm; produces a TileAA `reduce` with the same body region. |
| MMA (`mmaf`, `mmai`) | Builds result from A/B/C tile types; signedness attributes preserved. | Rank, K/M/N dimension agreement, accumulator/result type match, signedness presence for integer MMA. | Arithmetic-group arm; lowers to TileAA `dot` with optional scale-factor operands. |
| Constants and diagnostics (`constant`, `select`, `assert`, `assume`, `print`) | Constants carry a typed attribute; select carries condition plus two values; diagnostics carry a message and operand list. | Constant-attribute type match; select arm-type match; assume-predicate interface checks. | Constant-and-select arm (constants are constant-folded into the TileAA constant pool). |

The default builder for trivial unary ops shares one trampoline that constructs the op from a single operand and forwards rounding/flush-to-zero attributes; the default verifier hook installs a no-op stub when the op's contract is fully covered by trait-level checks. The control-flow lowering routes through one driver that owns `cuda_tile.if`, `cuda_tile.for`, `cuda_tile.loop`, `cuda_tile.continue`, and `cuda_tile.return` together so it can preserve region nesting and the structured-exit ancestry contract.

One count discrepancy is worth flagging. The roster in this build is 92 mnemonics. The two names missing from open-source documentation are `cuda_tile.atan2` (excluded entirely from this binary) and the rename `cuda_tile.print_tko` → `cuda_tile.print`. Producers should follow the version notes above and emit only the 92 mnemonics this dialect accepts.

The dialect constructor walks the registration thunks in roster order; each thunk interns the mnemonic into the dialect's `OperationName` table and installs the op's vtable, fold callback, and verifier hook through the slots described in [overview — AbstractOperation Record](overview.md#abstractoperation-record) and [Operation Layout — Pointer-Identity Dispatch](../../mlir-infra/operation-layout.md#pointer-identity-dispatch). Lowering patterns are matched as conversion patterns by the arithmetic-group and pointer-cast dispatchers during the first lowering stage; the conversion is documented in [Cuda Tile to TileAA](../../lowering/cuda-tile-to-tileaa.md#three-populator-structure).

## Cross-References

[Overview](overview.md#programming-model) describes the dialect's role as the public producer-facing API and the AbstractOperation record structure. [Verifiers](verifiers.md#verification-pipeline) details the verbatim verifier diagnostics each family emits. [Canonicalizers and Folds](canonicalizers-and-folds.md#fold-surface) describes the rewrites applied after verification. [Bytecode Reader and Writer](bytecode.md#op-opcode-dispatcher) documents the on-wire encoding the opcode dispatcher consumes.
